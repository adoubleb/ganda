"""The path IR (`ir_path`): the vertex count of a shortest path between two records
within a depth bound, through every engine path each SAP HANA data model has.

The answer is one row `{v: d + 1}` for the distance `d`, or no row when the goal is out
of reach or beyond the bound `k`. Every rendering computes the distance as the least
`d` in 0..k for which a WALK of length `d` from the start ends at the goal (a walk of
length `d` exists exactly when the distance is at most `d`), and answers no row over no
level:

    data model   syntax     the levels
    rel    levels     level d as a join of d copies of the relationship relation
           frontier   level d as `:b IN (<the records d hops out>)`, nested subqueries
    doc    levels     the same over the views of the link arrays (no frontier: the
                      Document Store refuses an UNNEST view nested inside IN (subquery))
    rdf    link       level d as a sequence of d property paths over the direct links
                      (`l:e/l:e`; `^l:e` incoming, `l:e|^l:e` both; every edge type
                      an alternative)
           resource   level d through the relationship resources (`p:src` / `p:dst`)
           table      `link` inside SQL's SPARQL_TABLE
    graph  range      level d as an openCypher variable-length path of exactly d
                      relationships (`[*d..d]`, `[:e*d..d]` for one type; in: the
                      pattern reversed; both: undirected) inside OPENCYPHER_TABLE --
                      a shortest walk is a simple path, so the path's own repetition
                      rule does not matter
           chain      level d as d one-hop patterns of one MATCH clause
           views      rel's `levels` over the `g_` CTEs reading the junctions through
                      the graph engine (`frontier` too, where the engine nests the
                      table function inside IN (...))
           script     GraphScript's `Shortest_Path` over the keys-only workspace `gws`
                      (`mapping.graph.script_ddl`; the subgraph of one edge KIND for one
                      type), its vertices counted in a SQLScript block -- compared live
                      only (the stand-in has no GraphScript)
    every  hint_<profile>  the data model's SQL levels (rel, doc, the graph views, rdf's
                      `table`) closed by a `WITH HINT (...)` clause steering them to
                      another execution engine or optimizer mode (`hints.PROFILES`),
                      one profile per question

The relationship relation of a question: one edge type's junction or every type's,
oriented by the direction (both: each relationship in both orientations).
"""
from __future__ import annotations

import json

from ganda.core.query.ir.path import PathQuery
from ganda.core.data.model import Instance, Schema
from ganda.hana.concrete_syntax import document, graph, hints
from ganda.hana.concrete_syntax.predicate import REL, Names
from ganda.hana.concrete_syntax.traversal import sparql_table
from ganda.hana.dialect import GRAPH_TOKEN, PREFIX_TEXT, record_iri

ID = "NVARCHAR(256)"


def bind_vars(q: PathQuery) -> dict:
    return {"a": q.start, "b": q.goal}


def _types(q: PathQuery, schema: Schema) -> list:
    return [q.edge_type] if q.edge_type else [et.name for et in schema.edge_types]


def _edges_sql(q: PathQuery, schema: Schema, names: Names) -> str:
    parts = []
    for e in _types(q, schema):
        rel = names.table(e)
        if q.direction in ("OUT", "BOTH"):
            parts.append(f"SELECT src_id AS a, dst_id AS b FROM {rel}")
        if q.direction in ("IN", "BOTH"):
            parts.append(f"SELECT dst_id AS a, src_id AS b FROM {rel}")
    return "(" + " UNION ALL ".join(parts) + ")"


def _sql(q: PathQuery, schema: Schema, syntax: str, names: Names) -> str:
    edges = _edges_sql(q, schema, names)
    levels = [f"SELECT 0 AS d FROM DUMMY WHERE CAST(:a AS {ID}) = CAST(:b AS {ID})"]
    for d in range(1, q.depth + 1):
        if syntax == "frontier":
            reach = f"SELECT x.b FROM {edges} x WHERE x.a = :a"
            for _ in range(d - 1):
                reach = f"SELECT x.b FROM {edges} x WHERE x.a IN ({reach})"
            levels.append(f"SELECT {d} AS d FROM DUMMY WHERE CAST(:b AS {ID}) IN ({reach})")
        else:
            joins = " ".join(f"JOIN {edges} x{i} ON x{i}.a = x{i - 1}.b" for i in range(2, d + 1))
            levels.append(f"SELECT {d} AS d FROM {edges} x1 {joins} WHERE x1.a = :a AND x{d}.b = :b".replace("  ", " "))
    union = " UNION ALL ".join(levels)
    return f"SELECT z.v AS v FROM (SELECT MIN(lv.d) + 1 AS v FROM ({union}) lv) z WHERE z.v IS NOT NULL"


def _hop(q: PathQuery, schema: Schema, u: str, w: str, syntax: str, i: int) -> str:
    types = _types(q, schema)
    if syntax == "resource":
        typed = "" if q.edge_type is None else f" a c:{q.edge_type} ;"
        out = f"?x{i}{typed} p:src {u} ; p:dst {w} ."
        inc = f"?x{i}{typed} p:dst {u} ; p:src {w} ."
        if q.direction == "OUT":
            return out
        if q.direction == "IN":
            return inc
        return f"{{ {out} }} UNION {{ {inc} }}"
    alt = "|".join(f"l:{e}" for e in types)
    path = {"OUT": f"({alt})", "IN": f"^({alt})", "BOTH": "(" + "|".join([f"l:{e}" for e in types] + [f"^l:{e}" for e in types]) + ")"}[q.direction]
    return f"{u} {path} {w} ."


def _sparql(q: PathQuery, schema: Schema, syntax: str) -> str:
    a, b = record_iri(q.start), record_iri(q.goal)
    levels = [f"{{ ?z p:id ?zid . FILTER (?z = {a} && ?z = {b}) BIND (0 AS ?d) }}"]
    for d in range(1, q.depth + 1):
        nodes = [a] + [f"?y{i}_{d}" for i in range(1, d)] + [b]
        hops = " ".join(_hop(q, schema, nodes[i], nodes[i + 1], syntax, i + 1) for i in range(d))
        if syntax == "resource":
            hops = hops.replace("?x", f"?x{d}_")
        levels.append(f"{{ {hops} BIND ({d} AS ?d) }}")
    union = " UNION ".join(levels)
    inner = f"SELECT (MIN(?d) + 1 AS ?v) WHERE {{ GRAPH {GRAPH_TOKEN} {{ {union} }} }}"
    return f"{PREFIX_TEXT} SELECT ?v WHERE {{ {{ {inner} }} FILTER (BOUND(?v)) }}"


def _cypher_levels(q: PathQuery, schema: Schema, syntax: str) -> str:
    """The levels through OPENCYPHER_TABLE, in the SQL frame of `_sql`: level d a
    variable-length path of exactly d relationships (`range`) or d one-hop MATCH
    clauses (`chain`), between the start and the goal bound as `$a` / `$b`."""
    params = graph.Params()
    A, B = graph.label(q.start_type), graph.label(q.goal_type)
    typed = f":{graph.label(q.edge_type)}" if q.edge_type else ""
    levels = [f"SELECT 0 AS d FROM DUMMY WHERE CAST(:a AS {ID}) = CAST(:b AS {ID})"]
    for d in range(1, q.depth + 1):
        if syntax == "range":
            edge = f"[{typed}*{d}..{d}]"
            pattern = {"OUT": f"(a:{A})-{edge}->(b:{B})", "IN": f"(b:{B})-{edge}->(a:{A})", "BOTH": f"(a:{A})-{edge}-(b:{B})"}[q.direction]
            match = f"MATCH rp = {pattern}"
        else:
            # d one-hop patterns of ONE clause (a heterogeneous workspace takes no second
            # clause, G8); a relationship binds once there, which a shortest path never
            # needs twice
            nodes = [f"a:{A}"] + [f"y{i}" for i in range(1, d)] + [f"b:{B}"]
            patterns = []
            for i in range(d):
                u, w, e = f"({nodes[i]})", f"({nodes[i + 1]})", f"[x{i + 1}{typed}]"
                patterns.append({"OUT": f"{u}-{e}->{w}", "IN": f"{w}-{e}->{u}", "BOTH": f"{u}-{e}-{w}"}[q.direction])
            match = "MATCH " + ", ".join(patterns)
        cypher = f"{match} WHERE a.ID = {params.use('a')} AND b.ID = {params.use('b')} RETURN a.ID AS ID"
        levels.append(f"SELECT {d} AS d FROM {graph.table_fn(cypher, params)}")
    union = " UNION ALL ".join(levels)
    return f"SELECT z.v AS v FROM (SELECT MIN(lv.d) + 1 AS v FROM ({union}) lv) z WHERE z.v IS NOT NULL"


def _graphscript(q: PathQuery) -> str:
    """GraphScript's `Shortest_Path` (probe G35) through a SQLScript block: the procedure
    of `mapping.graph` answers the path's vertices, counted here (`d + 1`), kept within
    the bound by HAVING (no row for an unreachable goal: an empty path). Over one edge
    type the procedure walks the subgraph of that KIND, which holds only the vertices
    with an edge of it, so the block first counts the ends' incident edges of the kind
    (start = goal is one vertex regardless) and answers nothing when either has none."""
    direction = {"OUT": "OUTGOING", "IN": "INCOMING", "BOTH": "ANY"}[q.direction]
    header = f"IN sv_a {ID} => :a, IN sv_b {ID} => :b"
    within = f"HAVING COUNT(*) BETWEEN 1 AND {q.depth + 1}"
    if q.edge_type is None:
        return f"DO ({header}) BEGIN CALL p_gs_path(:sv_a, :sv_b, '{direction}', sv_o); SELECT COUNT(*) AS v FROM :sv_o {within}; END"
    kind = q.edge_type
    incident = "FROM gs_e WHERE kind = '{kind}' AND (src_id = :{v} OR dst_id = :{v})"
    body = (
        "DECLARE sv_na BIGINT; DECLARE sv_nb BIGINT; "
        f"SELECT COUNT(*) INTO sv_na {incident.format(kind=kind, v='sv_a')}; "
        f"SELECT COUNT(*) INTO sv_nb {incident.format(kind=kind, v='sv_b')}; "
        "IF :sv_a = :sv_b THEN SELECT 1 AS v FROM DUMMY; "
        f"ELSEIF :sv_na > 0 AND :sv_nb > 0 THEN CALL p_gs_path_kind(:sv_a, :sv_b, '{kind}', '{direction}', sv_o); SELECT COUNT(*) AS v FROM :sv_o {within}; "
        "ELSE SELECT 1 AS v FROM DUMMY WHERE 1 = 0; END IF;"
    )
    return f"DO ({header}) BEGIN {body} END"


def render_all(q: PathQuery, schema: Schema, instance: Instance) -> "tuple[dict, dict]":
    queries = {
        "rel": _sql(q, schema, "levels", REL),
        "rel:frontier": _sql(q, schema, "frontier", REL),
        "doc": document.with_views(schema, _sql(q, schema, "levels", document.NAMES)),
        # no doc:frontier -- the frontier form nests the edge source (a collection view
        # with UNNEST) inside IN (subquery), which the Document Store refuses ("unsupported
        # form of subquery with UNNEST"); the levels form covers the doc data model's path
        "graph": _cypher_levels(q, schema, "range" if graph.VARLEN_OK else "chain"),
        "graph:views": graph.with_views(schema, _sql(q, schema, "levels", graph.NAMES)),
        "rdf": _sparql(q, schema, "link"),
        "rdf:resource": _sparql(q, schema, "resource"),
        "rdf:table": sparql_table(_sparql(q, schema, "link")),
    }
    if graph.VARLEN_OK:
        queries["graph:chain"] = _cypher_levels(q, schema, "chain")
    if graph.NESTED_TABLE_FN_OK:
        queries["graph:frontier"] = graph.with_views(schema, _sql(q, schema, "frontier", graph.NAMES))
    queries["graph:script"] = _graphscript(q)
    profile = hints.choose(json.dumps(q.to_params(), sort_keys=True, default=repr))
    for dm, base in (("rel", "rel"), ("doc", "doc"), ("graph", "graph:views"), ("rdf", "rdf:table")):
        queries[f"{dm}:hint_{profile}"] = hints.hinted(queries[base], profile)
    return queries, {}
