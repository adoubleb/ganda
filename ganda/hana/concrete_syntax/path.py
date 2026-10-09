"""The path AST (`ir_path`): the vertex count of a shortest path between two records
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

The relationship relation of a question: one edge type's junction or every type's,
oriented by the direction (both: each relationship in both orientations).
"""
from __future__ import annotations

from ganda.core.ast.path import PathQuery
from ganda.core.model import Instance, Schema
from ganda.hana.concrete_syntax import document
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


def render_all(q: PathQuery, schema: Schema, instance: Instance) -> "tuple[dict, dict]":
    queries = {
        "rel": _sql(q, schema, "levels", REL),
        "rel:frontier": _sql(q, schema, "frontier", REL),
        "doc": document.with_views(schema, _sql(q, schema, "levels", document.NAMES)),
        # no doc:frontier -- the frontier form nests the edge source (a collection view
        # with UNNEST) inside IN (subquery), which the Document Store refuses ("unsupported
        # form of subquery with UNNEST"); the levels form covers the doc data model's path
        "rdf": _sparql(q, schema, "link"),
        "rdf:resource": _sparql(q, schema, "resource"),
        "rdf:table": sparql_table(_sparql(q, schema, "link")),
    }
    return queries, {}
