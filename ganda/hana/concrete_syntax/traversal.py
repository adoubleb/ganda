"""The concrete syntax catalog of the traversal AST: HOW each SAP HANA data model asks a
`TraversalQuery`. Every rendering is a variant the oracle compares with every other.

    data model   syntax    the engine path
    rel    join      the path as a flat inner join of the junction and node tables
                     (aggregates: the start table LEFT JOINed to the paths as a derived
                     table and grouped; `exists`: a correlated EXISTS)
           scalar    the paths as a derived table joined to the start table;
                     aggregates as a correlated scalar subquery per start; `exists` as
                     `id [NOT] IN (SELECT start ...)`
    rdf    graph     a `GRAPH <g> { ... }` pattern over the relationship resources
                     (aggregates: the path in an OPTIONAL, grouped by the start)
           from      the same pattern with the graph named by `FROM <g>` (the
                     dataset's default graph) instead of a GRAPH block
           link      the DIRECT link triples (`?s l:e ?t`, property paths `^l:e` and
                     `l:e|^l:e`): set semantics, so the distinct questions only, with
                     no edge predicate
           minus     `nexists` through MINUS instead of FILTER NOT EXISTS
           table     the graph text inside SQL: `SELECT ... FROM SPARQL_TABLE('...')`
           native    `graph` with the filters' unknowns left to SPARQL's error
                     propagation (`predicate.filter_expr`) instead of spelled out
    doc    join, scalar   the rel syntaxes over the doc data model's VIEWS: every
                     collection read relationally by a collection query (its fields
                     projected and typed, its embedded link arrays and LIST arrays
                     UNNESTed), joined in SQL (`concrete_syntax.document`) -- the
                     engine boundary between the document store and SQL. `scalar` is
                     withheld for an aggregate or a reach count: a correlated scalar
                     subquery over an UNNEST view crashes the Document Store (H2,
                     `applicable`); the `join` form answers the same shape.

The `strt` alias (not `start`): START is a reserved word in HANA. An rdf float value is
read through `STR` and an int aliased `__int` (`_rdf_proj`): a SPARQL double term is
serialized lossily (H1) and SPARQL_TABLE surfaces a nullable number as NVARCHAR.

A reach question (the distinct records within k hops along one self-typed edge type,
the start included) is a union of the k + 1 levels on `rel` and a sequence of k
zero-or-one property paths (`l:e?/l:e?`) on `rdf` (`link`), or the levels' union
(`graph`).

Every syntax also has a PULLED-UP form (`render(..., pull_up=True)`, the
`<data model>:pull_up` variant): the source predicate is lifted out into an enclosing
query over the syntax projecting the source fields (`SELECT ... FROM (<syntax>) x
WHERE <predicate>` / a SPARQL subquery under a FILTER), the same question through a
plan that cannot push the predicate into the start's scan.
"""
from __future__ import annotations

from ganda.core.ast.traversal import AGG_PROJECTIONS, EDGE_AGG_PROJECTIONS, EDGE_PROJECTIONS, REACH_PROJECTIONS, SET_PROJECTIONS, TraversalQuery
from ganda.core.model import FieldType, Schema
from ganda.hana.concrete_syntax import document
from ganda.hana.concrete_syntax.predicate import REL, UNKNOWN_VAR, Names, fields_used, filter_expr, optionals, sparql_filter, sql, var
from ganda.hana.dialect import GRAPH_TOKEN, PREFIX_TEXT

SYNTAXES = {
    "doc": ("join", "scalar"),
    "rel": ("join", "scalar"),
    "rdf": ("graph", "from", "link", "minus", "table", "native"),
}
_AGGREGATES = ("count", "distinct_count") + AGG_PROJECTIONS + EDGE_AGG_PROJECTIONS
_DISTINCT = ("distinct_ids", "distinct_count", "exists", "nexists")


def _target_type(schema: Schema, hop) -> str:
    et = schema.edge_type(hop.edge_type)
    return et.to_type if hop.direction != "in" else et.from_type


def applicable(q: TraversalQuery, schema: Schema, data_model: str, syntax: str) -> bool:
    if data_model == "doc":
        if any(document.compares_list_text(p) for p in q.predicates):
            return False
        # the `scalar` form of an aggregate or a reach count is a correlated scalar
        # subquery over the collection views; the Document Store crashes on it (an
        # internal "Execution flow must not reach here", code 3) -- the `join` form
        # (a LEFT JOIN grouped by the start) answers the same shape
        if syntax == "scalar" and (q.projection in _AGGREGATES or (q.projection in REACH_PROJECTIONS and q.projection.endswith("_count"))):
            return False
        return True
    if data_model == "rel":
        return True
    if syntax == "link":
        if q.projection in REACH_PROJECTIONS:
            return True
        return q.projection in _DISTINCT and all(h.edge_predicate is None for h in q.hops)
    if syntax == "minus":
        return q.projection == "nexists"
    if syntax == "native":
        return bool(q.predicates)
    if syntax == "graph" and q.projection in REACH_PROJECTIONS:
        return True
    return True


def syntaxes_for(q: TraversalQuery, schema: Schema, data_model: str) -> list:
    return [s for s in SYNTAXES[data_model] if applicable(q, schema, data_model, s)]


def can_pull_up(q: TraversalQuery) -> bool:
    return q.source_predicate is not None


def render(q: TraversalQuery, schema: Schema, data_model: str, syntax: str, pull_up: bool = False) -> str:
    if data_model == "doc":
        return document.with_views(schema, _rel(q, schema, syntax, pull_up, document.NAMES))
    if data_model == "rel":
        return _rel(q, schema, syntax, pull_up)
    return _rdf(q, schema, syntax, pull_up)


def _source_fields(q: TraversalQuery) -> list:
    from ganda.core.ast.predicates import fields_of

    return fields_of(q.source_predicate)


# --- rel --------------------------------------------------------------------------------------


def _rel_value(q: TraversalQuery) -> str:
    k = len(q.hops)
    if q.edge_projection:
        return f"j{k}.{q.field}"
    if q.field is not None and q.projection not in ("ids", "distinct_ids", "count", "distinct_count") + SET_PROJECTIONS:
        return f"t{k}.{q.field}"
    return f"t{k}.id"


def _rel_paths(q: TraversalQuery, schema: Schema, names: Names, anchor: "str | None" = None) -> "tuple[str, str, list]":
    """(the FROM clause of the paths starting at the first junction -- or, with
    `anchor`, the JOINs that continue a FROM clause from the start column `anchor` --
    the start column, the path's predicates)."""
    parts, conds = [], []
    start = ""
    for i, hop in enumerate(q.hops, 1):
        et = schema.edge_type(hop.edge_type)
        tgt = _target_type(schema, hop)
        j, t = f"j{i}", f"t{i}"
        if hop.direction == "both":
            attrs = "".join(f", {f.name}" for f in et.fields)
            rel = f"(SELECT eid, src_id AS a, dst_id AS b{attrs} FROM {names.table(et.name)} UNION ALL SELECT eid, dst_id AS a, src_id AS b{attrs} FROM {names.table(et.name)}) {j}"
            near, far = "a", "b"
        else:
            rel = f"{names.table(et.name)} {j}"
            near, far = ("src_id", "dst_id") if hop.direction == "out" else ("dst_id", "src_id")
        if i == 1:
            parts.append(f"JOIN {rel} ON {j}.{near} = {anchor}" if anchor else rel)
            start = f"{j}.{near}"
        else:
            parts.append(f"JOIN {rel} ON {j}.{near} = t{i - 1}.id")
        parts.append(f"JOIN {names.table(tgt)} {t} ON {t}.id = {j}.{far}")
        if hop.edge_predicate is not None:
            conds.append(sql(hop.edge_predicate, j, et.name, "eid", names))
        if hop.target_predicate is not None:
            conds.append(sql(hop.target_predicate, t, tgt, "id", names))
    return " ".join(parts), start, conds


def _where(conds: list) -> str:
    conds = [c for c in conds if c]
    return f" WHERE {' AND '.join(conds)}" if conds else ""


def _rel_reach(q: TraversalQuery, schema: Schema, names: Names) -> str:
    """The derived table of (start, record within k hops), the start included. The start
    column is aliased `strt`, not `start`: HANA reserves START (START WITH / START
    TRANSACTION) and rejects it as an identifier."""
    e = names.table(q.hops[0].edge_type)
    levels = [f"SELECT id AS strt, id AS v FROM {names.table(q.source_table)}"]
    for d in range(1, q.reach_depth + 1):
        joins = " ".join(f"JOIN {e} j{i} ON j{i}.src_id = j{i - 1}.dst_id" for i in range(2, d + 1))
        levels.append(f"SELECT j1.src_id AS strt, j{d}.dst_id AS v FROM {e} j1 {joins}".rstrip())
    return " UNION ALL ".join(levels)


def _rel_agg(q: TraversalQuery, value: str) -> str:
    proj = q.projection.lstrip("e") if q.projection in EDGE_AGG_PROJECTIONS else q.projection
    if proj == "count":
        return f"COUNT({value})"
    if proj == "distinct_count":
        return f"COUNT(DISTINCT {value})"
    return f"{proj.upper()}({value})"


def _rel(q: TraversalQuery, schema: Schema, syntax: str, pull_up: bool, names: Names = REL) -> str:
    """The SQL text over the column tables -- or, with the doc data model's `names`,
    over the views of its collections."""
    src_pred = q.source_predicate
    keep = _source_fields(q) if pull_up else []
    extra = "".join(f", s.{f}" for f in keep)
    src_where = [] if pull_up or src_pred is None else [sql(src_pred, "s", q.source_table, "id", names)]
    S = f"{names.table(q.source_table)} s"
    proj = q.projection
    if proj in REACH_PROJECTIONS:
        reach = _rel_reach(q, schema, names)
        if proj.endswith("_count"):
            if syntax == "scalar":
                inner = f"SELECT s.id AS id, (SELECT COUNT(DISTINCT r.v) FROM ({reach}) r WHERE r.strt = s.id) AS v{extra} FROM {S}{_where(src_where)}"
            else:
                inner = f"SELECT s.id AS id, COUNT(DISTINCT r.v) AS v{extra} FROM {S} JOIN ({reach}) r ON r.strt = s.id{_where(src_where)} GROUP BY s.id{extra}"
        else:
            inner = f"SELECT DISTINCT s.id AS id, r.v AS v{extra} FROM {S} JOIN ({reach}) r ON r.strt = s.id{_where(src_where)}"
        return _rel_pull_up(q, inner, names) if pull_up else inner
    paths, start, conds = _rel_paths(q, schema, names)
    value = _rel_value(q)
    if proj in SET_PROJECTIONS:
        neg = "NOT " if proj == "nexists" else ""
        if syntax == "scalar":
            cond = f"s.id {neg}IN (SELECT {start} FROM {paths}{_where(conds)})"
        else:
            cond = f"{neg}EXISTS (SELECT 1 FROM {paths}{_where(conds + [f'{start} = s.id'])})"
        inner = f"SELECT s.id AS id{extra} FROM {S}{_where(src_where + [cond])}"
    elif proj in _AGGREGATES:
        agg = _rel_agg(q, "p.v")
        derived = f"SELECT {start} AS strt, {value} AS v FROM {paths}{_where(conds)}"
        if syntax == "scalar":
            inner = f"SELECT s.id AS id, (SELECT {agg} FROM ({derived}) p WHERE p.strt = s.id) AS v{extra} FROM {S}{_where(src_where)}"
        else:
            inner = f"SELECT s.id AS id, {agg} AS v{extra} FROM {S} LEFT JOIN ({derived}) p ON p.strt = s.id{_where(src_where)} GROUP BY s.id{extra}"
    else:
        distinct = "DISTINCT " if proj == "distinct_ids" else ""
        if syntax == "scalar":
            derived = f"SELECT {start} AS strt, {value} AS v FROM {paths}{_where(conds)}"
            inner = f"SELECT {distinct}s.id AS id, p.v AS v{extra} FROM {S} JOIN ({derived}) p ON p.strt = s.id{_where(src_where)}"
        else:
            joins, _start, conds = _rel_paths(q, schema, names, anchor="s.id")
            inner = f"SELECT {distinct}s.id AS id, {value} AS v{extra} FROM {S} {joins}{_where(src_where + conds)}"
    return _rel_pull_up(q, inner, names) if pull_up else inner


def _rel_pull_up(q: TraversalQuery, inner: str, names: Names) -> str:
    cols = "x.id AS id" if q.projection in SET_PROJECTIONS else "x.id AS id, x.v AS v"
    return f"SELECT {cols} FROM ({inner}) x WHERE {sql(q.source_predicate, 'x', q.source_table, 'id', names)}"


# --- rdf --------------------------------------------------------------------------------------


def _rdf_hops(q: TraversalQuery, schema: Schema, syntax: str, native: bool = False) -> str:
    out = []
    for i, hop in enumerate(q.hops, 1):
        prev = "?s" if i == 1 else f"?t{i - 1}"
        e = hop.edge_type
        j, t = f"?j{i}", f"?t{i}"
        if syntax == "link":
            path = {"out": f"l:{e}", "in": f"^l:{e}", "both": f"(l:{e}|^l:{e})"}[hop.direction]
            out.append(f"{prev} {path} {t} .")
        elif hop.direction == "both":
            out.append(f"{{ {j} a c:{e} ; p:src {prev} ; p:dst {t} . }} UNION {{ {j} a c:{e} ; p:dst {prev} ; p:src {t} . }}")
        else:
            near, far = ("src", "dst") if hop.direction == "out" else ("dst", "src")
            out.append(f"{j} a c:{e} ; p:{near} {prev} ; p:{far} {t} .")
        if hop.edge_predicate is not None:
            out.append(sparql_filter(hop.edge_predicate, j[1:], native=native))
        if hop.target_predicate is not None:
            out.append(sparql_filter(hop.target_predicate, t[1:], native=native))
    return " ".join(out)


def _rdf_value_pattern(q: TraversalQuery, var: str) -> str:
    """The triple binding `var` to the projected value of a path's end."""
    k = len(q.hops)
    if q.edge_projection:
        return f"?j{k} p:{q.field} {var} ."
    if q.projection in ("field",) + AGG_PROJECTIONS:
        return f"?t{k} p:{q.field} {var} ."
    return f"?t{k} p:id {var} ."


def _proj_ftype(q: TraversalQuery, schema: Schema) -> "FieldType | None":
    """The FieldType of the projected field (last hop's edge attribute, or the final
    class's field), or None when the projection is not a field value (an id, a count)."""
    if q.field is None:
        return None
    last = q.hops[-1]
    if q.edge_projection:
        return schema.field(last.edge_type, q.field, edge=True).type
    et = schema.edge_type(last.edge_type)
    tgt = et.to_type if last.direction in ("out", "both") else et.from_type
    return schema.field(tgt, q.field).type


def _rdf_proj(ftype: "FieldType | None", var: str, alias: str) -> str:
    """A value variable as a SPARQL SELECT-list item carrying its decode suffix: a FLOAT
    through `STR` (`__float`, issues-draft/H1), an INT `__int` (SPARQL_TABLE surfaces a
    numeric var as an NVARCHAR column otherwise), a LIST `__json`; else the bare var."""
    if ftype is FieldType.FLOAT:
        return f"(STR(?{var}) AS ?{alias}__float)"
    if ftype is FieldType.INT:
        return f"(?{var} AS ?{alias}__int)"
    if ftype is FieldType.LIST:
        return f"(?{var} AS ?{alias}__json)"
    return f"?{var}" if var == alias else f"(?{var} AS ?{alias})"


def _rdf_reach(q: TraversalQuery, syntax: str) -> str:
    e = q.hops[0].edge_type
    if syntax == "link":
        return f"?s {'/'.join(f'l:{e}?' for _ in range(q.reach_depth))} ?r ."
    branches = ["BIND (?s AS ?r)"] + [f"?s {'/'.join(f'l:{e}' for _ in range(d))} ?r ." for d in range(1, q.reach_depth + 1)]
    return " UNION ".join(f"{{ ?s a c:{q.source_table} . {b} }}" for b in branches)


def _sparql(select: str, body: str, tail: str, syntax: str) -> str:
    if syntax == "from":
        return f"{PREFIX_TEXT} {select} FROM {GRAPH_TOKEN} WHERE {{ {body} }}{tail}"
    return f"{PREFIX_TEXT} {select} WHERE {{ GRAPH {GRAPH_TOKEN} {{ {body} }} }}{tail}"


def _rdf(q: TraversalQuery, schema: Schema, syntax: str, pull_up: bool) -> str:
    if syntax == "table":
        return sparql_table(_rdf(q, schema, "graph", pull_up))
    shape = "graph" if syntax in ("minus", "table", "native") else syntax
    native = syntax == "native"
    start = f"?s a c:{q.source_table} ; p:id ?id ."
    if pull_up:
        # the source fields bound inside the inner query and projected out of it; the
        # predicate over them filters the inner query's rows from outside
        used = fields_used(q.source_predicate, "s")
        carry = "?s " + " ".join(var(r, f) for r, f in used)
        head = f"{start} {optionals(q.source_predicate, 's')}".strip()
    else:
        carry = ""
        src = "" if q.source_predicate is None else sparql_filter(q.source_predicate, "s", native=native)
        head = f"{start} {src}".strip()
    keep = (" " + carry.strip()) if carry else ""
    proj = q.projection
    group = " GROUP BY ?id" + keep + " HAVING (COUNT(*) > 0)"
    vcol = "v"
    if proj in REACH_PROJECTIONS:
        reach = _rdf_reach(q, shape)
        body = f"{head} {reach} ?r p:id ?v ."
        if proj.endswith("_count"):
            body = f"{head} {reach}"
            select, tail = f"SELECT ?id{keep} (COUNT(DISTINCT ?r) AS ?v)", group
        else:
            select, tail = f"SELECT DISTINCT ?id ?v{keep}", ""
    elif proj in SET_PROJECTIONS:
        path = _rdf_hops(q, schema, shape, native)
        if syntax == "minus":
            body = f"{head} MINUS {{ {path} }}"
        else:
            body = f"{head} FILTER {'NOT ' if proj == 'nexists' else ''}EXISTS {{ {path} }}"
        select, tail = f"SELECT ?id{keep}", ""
    elif proj in _AGGREGATES:
        # the paths a subquery, left-joined to the starts: the path's filters stay
        # inside it, never a condition of the OPTIONAL
        path = _rdf_hops(q, schema, shape, native)
        k = len(q.hops)
        if proj in ("count", "distinct_count"):
            body = f"{head} OPTIONAL {{ SELECT ?s ?t{k} WHERE {{ {path} }} }}"
            agg, vcol = f"COUNT({'DISTINCT ' if proj == 'distinct_count' else ''}?t{k})", "v"
        else:
            body = f"{head} OPTIONAL {{ SELECT ?s ?w WHERE {{ {path} {_rdf_value_pattern(q, '?w')} }} }}"
            fn = proj.lstrip("e") if proj in EDGE_AGG_PROJECTIONS else proj
            agg = f"IF(COUNT(?w) > 0, SUM(?w), {UNKNOWN_VAR})" if fn == "sum" else f"{fn.upper()}(?w)"
            # max/min keep the field's type; sum of a float is a double -- STR for a float
            ft = _proj_ftype(q, schema)
            agg, vcol = (f"STR({agg})", "v__float") if ft is FieldType.FLOAT else (agg, "v__int" if ft is FieldType.INT else "v")
        select, tail = f"SELECT ?id{keep} ({agg} AS ?{vcol})", group
    else:
        path = _rdf_hops(q, schema, shape, native)
        value = _rdf_value_pattern(q, "?v")
        if proj in ("field", "efield"):
            value = f"OPTIONAL {{ {value[:-2]} }}"
        body = f"{head} {path} {value}"
        # field/efield project the field's own value (decode suffix); distinct_ids is an id
        vproj = _rdf_proj(_proj_ftype(q, schema), "v", "v") if proj in ("field", "efield") else "?v"
        vcol = vproj.split(" AS ?")[-1].rstrip(")") if " AS ?" in vproj else "v"
        select, tail = (f"SELECT DISTINCT ?id {vproj}" if proj == "distinct_ids" else f"SELECT ?id {vproj}") + keep, ""
    if pull_up:
        # the inner already exposes the (possibly suffixed) value column; the outer passes
        # it through unchanged
        outer = "SELECT ?id" if proj in SET_PROJECTIONS else f"SELECT ?id ?{vcol}"
        inner = f"{select} WHERE {{ {body} }}{tail}"
        body = f"{{ {inner} }} FILTER ({filter_expr(q.source_predicate, 's', native=native)})"
        select, tail = outer, ""
    return _sparql(select, body, tail, shape)


def sparql_table(text: str) -> str:
    """A SPARQL query as the SQL `SPARQL_TABLE` table function's argument: one column
    per result variable (the session lowercases the names)."""
    quoted = text.replace("'", "''")
    return f"SELECT * FROM SPARQL_TABLE('{quoted}')"
