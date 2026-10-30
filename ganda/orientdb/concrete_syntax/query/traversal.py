from __future__ import annotations

import dataclasses

from ganda.core.data.model import Schema
from ganda.core.query.ir.traversal import AGG_PROJECTIONS, EDGE_AGG_PROJECTIONS, EDGE_PROJECTIONS, FIELD_PROJECTIONS, REACH_PROJECTIONS, SET_PROJECTIONS, Hop, TraversalQuery
from ganda.core.query.ir.predicates import fields_of, has_op
from ganda.orientdb.concrete_syntax.query.predicate import condition, render_predicate as render_pred
from ganda.orientdb.concrete_syntax.query.scope import CURRENT, RECORD, RENAMED, Lookup, Path, Renamed, Semijoin

from ganda.orientdb.rules import answer, assumes, works_around
from ganda.orientdb.rules.catalog import ACCEPTED, MATCH, SQL, TRAVERSE, Strategy
from ganda.core.rules.vocabulary import EmptyAggregate, EmptyInputRows

SYNTAXES = {
    "graph": ("chain", "match", "match_opt", "edgescan", "bag", "traverse", "traverse_dfs", "traverse_e"),
    "doc": ("path", "unwind", "let_expand", "inpath", "union", "traverse"),
    "rel": ("let", "flat"),
}
# a correlated LET reads the enclosing record under a scan
assumes("equivalent.correlated_let", SQL, True)

_MULTI = ("ids", "field", "distinct_ids", "efield")
_DISTINCT = ("distinct_ids", "distinct_count", "exists", "nexists")


def _br(p) -> str:
    """A bracket filter `[f op :p]` over the elements of a collection."""
    return "" if p is None else f"[{render_pred(p)}]"


def _where(*conds) -> str:
    conds = [c for c in conds if c]
    return f" WHERE {' AND '.join(conds)}" if conds else ""


def _sp(q: TraversalQuery, at: Path = RECORD) -> str:
    p = q.source_predicate
    return "" if p is None else render_pred(p, at)


def _cols(extra: tuple, at: Path = RECORD, alias: bool = False) -> str:
    """The extra source columns a pulled-up inner query projects, as `, f1, f2` (or
    `, a.f1 AS f1` when `alias`), read off the source record `at` names."""
    if not extra:
        return ""
    return ", " + ", ".join(f"{at.field(f)} AS {f}" if alias else at.field(f) for f in extra)


def _group(extra: tuple) -> str:
    return " GROUP BY id" + "".join(f", {f}" for f in extra)




# a bare-set projection's `.size() = 0` / `> 0` also covers a source with no link list
assumes("value.absent_list.size", SQL, 0)


def _set_filter(expr: str, q: TraversalQuery) -> str:
    """The WHERE condition of a bare-set projection over collection `expr`."""
    return f"{expr}.size() > 0" if q.projection == "exists" else f"{expr}.size() = 0"


# --- applicability ----------------------------------------------------------------------------


# an embedded document expands under `value`; no MATCH negative pattern is rendered (O22)
assumes("expand.embedded.wrapped_under_value", SQL, True)
works_around("O22")


# rel:flat folds max / min, and a start record with no path answers null there
assumes("aggregate.max.empty_input", SQL, EmptyAggregate.NULL)
# a multi-hop doc:path maps field access element-wise through the link lists
assumes("path.link_list.maps_field_access", SQL, True)


def applicable(q: TraversalQuery, schema: Schema, data_model: str, syntax: str) -> bool:
    n = len(q.hops)
    first, last = q.hops[0], q.hops[-1]
    proj = q.projection
    has_in = any(h.direction == "in" for h in q.hops)
    both = q.has_both
    edge_proj = q.edge_projection
    if edge_proj and last.target_predicate is not None:
        return False
    if both and n != 1:
        return False
    if proj in REACH_PROJECTIONS:
        # the variable-depth question: one unpredicated outgoing hop, walked k times
        # by every executor that answers a distinct set (the start included)
        if n != 1 or first.direction != "out" or first.predicate is not None:
            return False
        if data_model == "graph":
            return syntax in GRAPH_REACH_SYNTAXES
        if data_model == "doc":
            return syntax in ("path", "traverse")
        return syntax == "let"
    if syntax in ("traverse_dfs", "traverse_e"):
        return False
    if data_model == "graph":
        if syntax in ("chain", "bag"):
            return True
        if syntax in ("match", "match_opt"):
            # `bothE().bothV()` doubles every row: a `both` hop under an edge predicate
            # has no MATCH form unless the pattern ends at the edge alias
            return proj in _MULTI and not (both and first.edge_predicate is not None and not edge_proj)
        if syntax == "edgescan":
            return proj in _MULTI and (n == 1 or not edge_proj)
        if syntax == "traverse":
            et = schema.edge_type(first.edge_type)
            return n == 1 and first.edge_predicate is None and proj in _DISTINCT and et.from_type != et.to_type and not both
    if data_model == "doc":
        if syntax == "path":
            return not has_in and not both and (n == 1 or proj in _MULTI)
        if syntax == "unwind":
            return not has_in and not both and n == 1 and proj in ("ids", "field", "efield")
        if syntax == "let_expand":
            return not has_in and not both and n == 1 and not edge_proj
        if syntax == "inpath":
            return n == 1 and first.direction == "in"
        if syntax == "union":
            return n == 1 and both
    if data_model == "rel":
        if syntax == "let":
            return n == 1 or proj in _MULTI or proj in SET_PROJECTIONS
        if syntax == "flat":
            if proj == "nexists":
                return n == 1
            if both:
                return proj in _MULTI or proj == "exists"
            return proj in _MULTI or proj in ("max", "min", "emax", "emin", "exists")
    return False


# IS NULL holds for an absent field as for a null one: a pulled-up predicate meets nulls


_CI_OPS = tuple(answer("filter.case_insensitive.operators", SQL).split())


# a pull-up lifts the source predicate over a projection on doc and graph, and keeps it
# on the class on rel (`id IN (SELECT id FROM t WHERE p)`): a predicate the projection
# changes would make them differ
assumes("projection.keeps_absent_field", SQL, False)
# IS NULL holds for an absent field as for a null one: a pulled-up predicate meets nulls
assumes("value.absent_is_null", SQL, True)


def can_pull_up(q: TraversalQuery) -> bool:
    """A source predicate can be lifted over a projection unless it tests
    definedness (an absent field projects as an explicit null) or compares a `ci`
    field (a projected value carries no collation: campaign c100, seed 670, an O25
    face; campaign p3, seed 1008, the definedness face)."""
    p = q.source_predicate
    if p is None or has_op(p, ("IS PRESENT", "IS ABSENT")):
        return False
    return not any(leaf.case_insensitive and leaf.op in _CI_OPS for leaf in p.leaves())


def syntaxes_for(q: TraversalQuery, schema: Schema, data_model: str) -> list:
    return [s for s in SYNTAXES[data_model] if applicable(q, schema, data_model, s)]


# --- projections ----------------------------------------------------------------------------


def _projected(expr: str, q: TraversalQuery, elem: "Path | Renamed" = RECORD, extra: tuple = ()) -> "tuple[str, str]":
    """(projection text, tail): the projection over the final collection `expr` and the
    GROUP BY tail an aggregate needs. `elem` is what an element of the collection is: a
    record or relationship reads its id and `q.field` itself, a LET subquery's row has
    already renamed its value `v` (`RENAMED`)."""
    proj = q.projection
    kind = proj[1:] if proj in EDGE_PROJECTIONS else proj
    if kind == "ids":
        return f"{expr}.{elem.id}", ""
    if kind == "distinct_ids":
        return f"{expr}.{elem.id}.asSet()", ""
    if kind == "count":
        return f"{expr}.size()", ""
    if kind == "distinct_count":
        return f"{expr}.{elem.id}.asSet().size()", ""
    if kind == "field":
        return f"{expr}.{elem.field(q.field)}", ""
    if kind in AGG_PROJECTIONS:
        return f"{kind}({expr}.{elem.field(q.field)})", _group(extra)
    raise ValueError(proj)


def _projected_flat(expr: str, q: TraversalQuery, extra: tuple = ()) -> "tuple[str, str]":
    """The projection over a FLAT collection of the projected values themselves (an
    id list, or a field list)."""
    proj = q.projection
    kind = proj[1:] if proj in EDGE_PROJECTIONS else proj
    if kind in ("ids", "field"):
        return expr, ""
    if kind == "distinct_ids":
        return f"{expr}.asSet()", ""
    if kind == "count":
        return f"{expr}.size()", ""
    if kind == "distinct_count":
        return f"{expr}.asSet().size()", ""
    if kind in AGG_PROJECTIONS:
        return f"{kind}({expr})", _group(extra)
    raise ValueError(proj)


# --- graph ----------------------------------------------------------------------------------


def _graph_hop(hop: Hop, bag: bool = False, edge_final: bool = False) -> str:
    et = hop.edge_type
    ep, tp = hop.edge_predicate, hop.target_predicate
    if hop.direction == "both":
        if bag:
            if edge_final:
                return f"unionall(out_{et}{_br(ep)}, in_{et}{_br(ep)})"
            return f"unionall(out_{et}{_br(ep)}.in{_br(tp)}, in_{et}{_br(ep)}.out{_br(tp)})"
        if edge_final:
            return f"bothE('{et}'){_br(ep)}"
        if ep is not None:
            return f"unionall(outE('{et}'){_br(ep)}.inV(){_br(tp)}, inE('{et}'){_br(ep)}.outV(){_br(tp)})"
        return f"both('{et}'){_br(tp)}"
    out = hop.direction == "out"
    if bag:
        fwd = "out_" if out else "in_"
        back = "in" if out else "out"
        if edge_final:
            return f"{fwd}{et}{_br(ep)}"
        return f"{fwd}{et}{_br(ep)}.{back}{_br(tp)}"
    if ep is not None or edge_final:
        fn, back = ("outE", "inV") if out else ("inE", "outV")
        if edge_final:
            return f"{fn}('{et}'){_br(ep)}"
        return f"{fn}('{et}'){_br(ep)}.{back}(){_br(tp)}"
    fn = "out" if out else "in"
    return f"{fn}('{et}'){_br(tp)}"


def _graph_expr(q: TraversalQuery, hops: tuple, bag: bool = False, edge_final: bool = False) -> str:
    n = len(hops)
    return ".".join(_graph_hop(h, bag, edge_final and k == n - 1) for k, h in enumerate(hops))


# out() / both() answer a multiset: parallel edges twice, a self-loop twice under both()
assumes("match.parallel_edges.rows", SQL, 2)
assumes("match.undirected.self_loop.rows", SQL, 2)


def _graph_chain(q: TraversalQuery, schema: Schema, bag: bool, extra: tuple = ()) -> str:
    expr = _graph_expr(q, q.hops, bag, q.edge_projection)
    src = q.source_table
    if q.projection in SET_PROJECTIONS:
        return f"SELECT id{_cols(extra)} FROM {src}{_where(_sp(q), _set_filter(expr, q))}"
    proj, tail = _projected(expr, q, extra=extra)
    return f"SELECT id{_cols(extra)}, {proj} AS v FROM {src}{_where(_sp(q))}{tail}"


assumes("match.parallel_edges.rows", MATCH, 2)
assumes("match.optional.no_path.rows", MATCH, EmptyInputRows.ONE_ROW)
# the class sits on the root alias only (O45), and a both-hop edge projection ends the
# pattern at an edge alias
works_around("O45")
assumes("match.classless_alias.is_root", MATCH, False)
assumes("accepts.match.end_at_edge_alias", MATCH, ACCEPTED)


def _graph_match(q: TraversalQuery, schema: Schema, extra: tuple = (), optional: bool = False) -> str:
    opt = ", optional: true" if optional else ""
    parts = [f"{{class: {q.source_table}, as: a" + (f", where: ({_sp(q)})" if q.source_predicate else "") + "}"]
    n = len(q.hops)
    for k, h in enumerate(q.hops, 1):
        ep, tp = h.edge_predicate, h.target_predicate
        twhere = f", where: ({render_pred(tp)})" if tp else ""
        if h.direction == "both":
            if q.edge_projection and k == n:
                ewhere = f", where: ({render_pred(ep)})" if ep else ""
                parts.append(f".bothE('{h.edge_type}') {{as: ae{k}{ewhere}{opt}}}")
            else:
                parts.append(f".both('{h.edge_type}') {{as: av{k}{twhere}{opt}}}")
            continue
        out = h.direction == "out"
        if ep is not None or (q.edge_projection and k == n):
            fn, back = ("outE", "inV") if out else ("inE", "outV")
            ewhere = f", where: ({render_pred(ep)})" if ep else ""
            parts.append(f".{fn}('{h.edge_type}') {{as: ae{k}{ewhere}{opt}}} .{back}() {{as: av{k}{twhere}{opt}}}")
        else:
            parts.append(f".{'out' if out else 'in'}('{h.edge_type}') {{as: av{k}{twhere}{opt}}}")
    distinct = " DISTINCT" if q.projection == "distinct_ids" else ""
    # in RETURN the aliases are prefixes; in a `where:` the alias's record is the current one
    a = Path("a.")
    if q.edge_projection:
        value = Path(f"ae{n}.").field(q.field)
    else:
        last = Path(f"av{n}.")
        value = last.field(q.field) if q.projection == "field" else last.id
    return f"MATCH {' '.join(parts)} RETURN{distinct} {a.id} AS id{_cols(extra, a, alias=True)}, {value} AS v"


def _graph_edgescan_part(q: TraversalQuery, out: bool, extra: tuple = ()) -> str:
    h = q.hops[0]
    near, far = ("out", "in") if out else ("in", "out")
    # the E class's record is the edge; the source and the target are its ends
    src, tgt = Path(f"{near}."), Path(f"{far}.")
    conds = []
    if h.edge_predicate:
        conds.append(render_pred(h.edge_predicate))
    if h.target_predicate:
        conds.append(render_pred(h.target_predicate, tgt))
    if q.source_predicate:
        conds.append(_sp(q, src))
    distinct = " DISTINCT" if q.projection == "distinct_ids" else ""
    if q.edge_projection:
        value = q.field
    else:
        rest = _graph_expr(q, q.hops[1:]) if len(q.hops) > 1 else ""
        expr = f"{far}.{rest}" if rest else far
        value = f"{expr}.{q.field if q.projection == 'field' else 'id'}"
    return f"SELECT{distinct} {src.id} AS id{_cols(extra, src, alias=True)}, {value} AS v FROM {h.edge_type}{_where(*conds)}"


def _graph_edgescan(q: TraversalQuery, schema: Schema, extra: tuple = ()) -> str:
    h = q.hops[0]
    if h.direction != "both":
        return _graph_edgescan_part(q, h.direction == "out", extra)
    a = _graph_edgescan_part(q, True, extra)
    b = _graph_edgescan_part(q, False, extra)
    # inline, not `LET $a = (...), $b = (...), $c = unionall($a, $b)`: that form
    # answers nothing when the parts carry a nested correlated LET (O26)
    return f"SELECT expand(unionall(({a}), ({b})))"


assumes("equivalent.unionall_inline", SQL, True)


# --- the variable-depth (reach) forms --------------------------------------------------------


def _reach_set(coll: str, q: TraversalQuery, extra: tuple, src: str, let: str = "") -> str:
    """`SELECT id, <coll>.asSet() AS v` / `.asSet().size()` over a flat id collection."""
    proj = f"{coll}.asSet()" if q.projection in ("reach2", "reach3") else f"{coll}.asSet().size()"
    return f"SELECT id{_cols(extra)}, {proj} AS v FROM {src}{(' ' + let) if let else ''}{_where(_sp(q))}"


def _graph_reach_chain(q: TraversalQuery, extra: tuple) -> str:
    et = q.hops[0].edge_type
    parts = ["[@this]"] + [".".join([f"out('{et}')"] * k) for k in range(1, q.reach_depth + 1)]
    return _reach_set(f"unionall({', '.join(parts)}).id", q, extra, q.source_table)


def _graph_reach_match(q: TraversalQuery, extra: tuple) -> str:
    et = q.hops[0].edge_type
    where = f", where: ({_sp(q)})" if q.source_predicate else ""
    inner = f"MATCH {{class: {q.source_table}, as: a{where}}}.out('{et}') {{as: b, while: ($depth < {q.reach_depth})}} RETURN DISTINCT a.id AS id{_cols(extra, Path('a.'), alias=True)}, b.id AS bid"
    if q.projection in ("reach2", "reach3"):
        return f"SELECT id{_cols(extra)}, bid AS v FROM ({inner})"
    return f"SELECT id{_cols(extra)}, count(*) AS v FROM ({inner}){_group(extra)}"


def _reach_traverse(q: TraversalQuery, extra: tuple, walk: str, strategy: str = "", tail: str = "") -> str:
    """The records TRAVERSE visits from the current one within `reach_depth` hops of
    `walk` (the start included): graph's vertex bags, doc's link lists."""
    let = f"LET $t = (SELECT FROM (TRAVERSE {walk} FROM $parent.$current MAXDEPTH {q.reach_depth}{strategy}){tail})"
    return _reach_set("$t.id", q, extra, q.source_table, let)


def _graph_reach_traverse(q: TraversalQuery, extra: tuple, strategy: str = "", fn: "str | None" = None) -> str:
    walk = fn or f"out('{q.hops[0].edge_type}')"
    # `outE().inV()` visits the edges too: the source class keeps the vertices
    return _reach_traverse(q, extra, walk, strategy, f" WHERE @class = '{q.source_table}'" if fn else "")


# the doc side walks its link lists: a link document adds no depth and answers no row
assumes("traverse.embedded_document.adds_depth", TRAVERSE, False)
assumes("traverse.embedded_document.answers_row", TRAVERSE, False)


def _doc_reach_traverse(q: TraversalQuery, extra: tuple) -> str:
    return _reach_traverse(q, extra, f"{q.hops[0].edge_type}, target", " STRATEGY BREADTH_FIRST")


GRAPH_REACH_SYNTAXES = ("chain", "match", "traverse", "traverse_dfs", "traverse_e")


# TRAVERSE walks depth-first unless told: the reach forms name BREADTH_FIRST (O44)
assumes("traverse.default_strategy", TRAVERSE, Strategy.DEPTH_FIRST)
works_around("O44")


def _graph_reach(q: TraversalQuery, syntax: str, extra: tuple) -> str:
    if syntax == "chain":
        return _graph_reach_chain(q, extra)
    if syntax == "match":
        return _graph_reach_match(q, extra)
    # the reach SET under a MAXDEPTH is complete only breadth-first: depth-first
    # (the default) marks a record visited at the depth it first meets it and
    # does not expand it there at the bound, so a record also reachable in
    # fewer hops keeps its children unvisited (O44, cov400 seeds 5242 / 5342);
    # `traverse_dfs` asks the depth-first executor where that cannot happen
    if syntax == "traverse":
        return _graph_reach_traverse(q, extra, " STRATEGY BREADTH_FIRST")
    if syntax == "traverse_dfs":
        return _graph_reach_traverse(q, extra, " STRATEGY DEPTH_FIRST")
    if syntax == "traverse_e":
        return _graph_reach_traverse(q, extra, " STRATEGY BREADTH_FIRST", f"outE('{q.hops[0].edge_type}').inV()")
    raise ValueError(("graph", syntax))


def _doc_reach(q: TraversalQuery, extra: tuple) -> str:
    """The path union `unionall([@this], e.target, e.target.e.target, ...)`: nested
    lists the oracle's set normalization flattens for the id set; for the count,
    one UNWIND per level flattens them before the DISTINCT and the `count(*)`."""
    et = q.hops[0].edge_type
    k = q.reach_depth
    parts = ["[@this]"] + [".".join([f"{et}.target"] * d) for d in range(1, k + 1)]
    coll = f"unionall({', '.join(parts)}).id"
    src = q.source_table
    if q.projection in ("reach2", "reach3"):
        return f"SELECT id{_cols(extra)}, {coll}.asSet() AS v FROM {src}{_where(_sp(q))}"
    inner = f"SELECT id{_cols(extra)}, {coll} AS v FROM {src}{_where(_sp(q))} UNWIND v"
    for _ in range(k - 1):
        inner = f"SELECT id{_cols(extra)}, v FROM ({inner}) UNWIND v"
    # an absent list unwinds to a null row (smoke4 seed 712): filtered before the DISTINCT
    return f"SELECT id{_cols(extra)}, count(*) AS v FROM (SELECT DISTINCT id{_cols(extra)}, v FROM ({inner}) WHERE v IS NOT NULL){_group(extra)}"


def _rel_reach_level(et: str, depth: int, ref: str = "id") -> str:
    """The junction subquery answering the records exactly `depth` hops away from
    the enclosing record (its endpoint column `ref`), unwound flat at every level."""
    cur = Path(CURRENT, ref)
    if depth == 1:
        return f"(SELECT dst_id AS v FROM {et} WHERE src_id = {cur.id})"
    inner = _rel_reach_level(et, depth - 1, "dst_id")
    # a junction row whose next level is empty is dropped (`$h.size() > 0`) rather
    # than unwound into a null row (smoke4b seed 712: the count took the null)
    return f"(SELECT $h{depth}.v AS v FROM {et} LET $h{depth} = {inner} WHERE src_id = {cur.id} AND $h{depth}.size() > 0 UNWIND v)"


def _rel_reach(q: TraversalQuery, extra: tuple) -> str:
    et = q.hops[0].edge_type
    lets = ", ".join(f"$g{d} = {_rel_reach_level(et, d)}" for d in range(1, q.reach_depth + 1))
    coll = "unionall([id], " + ", ".join(f"$g{d}.v" for d in range(1, q.reach_depth + 1)) + ")"
    return _reach_set(coll, q, extra, q.source_table, f"LET {lets}")


def _graph_traverse(q: TraversalQuery, schema: Schema, extra: tuple = ()) -> str:
    """`TRAVERSE out('e1') FROM $parent.$current MAXDEPTH 1`, the records at depth 1:
    the traverse executor over the same bags `chain` reads. It visits each record
    once, so it is admitted for the distinct questions only, and never over an edge
    type whose target class is its source class (a self-loop's target is the root,
    already visited)."""
    h = q.hops[0]
    fn = "out" if h.direction == "out" else "in"
    tp = h.target_predicate
    inner = f"SELECT FROM (TRAVERSE {fn}('{h.edge_type}') FROM $parent.$current MAXDEPTH 1) WHERE $depth = 1" + (f" AND {render_pred(tp)}" if tp else "")
    let = f"LET $t = ({inner})"
    src = q.source_table
    if q.projection in SET_PROJECTIONS:
        return f"SELECT id{_cols(extra)} FROM {src} {let}{_where(_sp(q), _set_filter('$t', q))}"
    proj = "$t.id.asSet()" if q.projection == "distinct_ids" else "$t.size()"
    return f"SELECT id{_cols(extra)}, {proj} AS v FROM {src} {let}{_where(_sp(q))}"


# --- doc ------------------------------------------------------------------------------------


def _doc_expr(hops: tuple, edge_final: bool = False) -> str:
    n = len(hops)
    parts = []
    for k, h in enumerate(hops):
        if edge_final and k == n - 1:
            parts.append(f"{h.edge_type}{_br(h.edge_predicate)}")
        else:
            parts.append(f"{h.edge_type}{_br(h.edge_predicate)}.target{_br(h.target_predicate)}")
    return ".".join(parts)


def _doc_path(q: TraversalQuery, schema: Schema, extra: tuple = ()) -> str:
    expr = _doc_expr(q.hops, q.edge_projection)
    src = q.source_table
    if q.projection in SET_PROJECTIONS:
        return f"SELECT id{_cols(extra)} FROM {src}{_where(_sp(q), _set_filter(expr, q))}"
    proj, tail = _projected(expr, q, extra=extra)
    return f"SELECT id{_cols(extra)}, {proj} AS v FROM {src}{_where(_sp(q))}{tail}"


def _doc_unwind(q: TraversalQuery, schema: Schema, extra: tuple = ()) -> str:
    col = q.field if q.projection in ("field", "efield") else "id"
    return f"SELECT id{_cols(extra)}, {_doc_expr(q.hops, q.edge_projection)}.{col} AS v FROM {q.source_table}{_where(_sp(q))} UNWIND v"


def _doc_let_expand(q: TraversalQuery, schema: Schema, extra: tuple = ()) -> str:
    src = q.source_table
    let = f"LET $x = (SELECT expand({_doc_expr(q.hops)}) FROM {src} WHERE @rid = $parent.$current.@rid)"
    if q.projection in SET_PROJECTIONS:
        return f"SELECT id{_cols(extra)} FROM {src} {let}{_where(_sp(q), _set_filter('$x', q))}"
    proj, tail = _projected("$x", q, extra=extra)
    return f"SELECT id{_cols(extra)}, {proj} AS v FROM {src} {let}{_where(_sp(q))}{tail}"


def _doc_in_subquery(q: TraversalQuery, schema: Schema) -> str:
    """The incoming hop as a subquery over the source class: every source record whose
    list holds a link to the current record, one row per such link (`UNWIND`), each
    row projecting `v` -- the source's id / field, or the matching link's attribute."""
    h = q.hops[0]
    et = h.edge_type
    src_class = schema.edge_type(et).from_type
    me = Path(CURRENT, "@rid").id
    elem = [f"target = {me}"]
    if h.edge_predicate:
        elem.append(render_pred(h.edge_predicate))
    conds = [f"{et} CONTAINS ({' AND '.join(elem)})"]
    if h.target_predicate:
        conds.append(render_pred(h.target_predicate))
    if q.edge_projection:
        return f"(SELECT {et}[{' AND '.join(elem)}].{q.field} AS v FROM {src_class}{_where(*conds)} UNWIND v)"
    col = q.field if q.projection in FIELD_PROJECTIONS else "id"
    return f"(SELECT {col} AS v, {et}[{' AND '.join(elem)}] AS m FROM {src_class}{_where(*conds)} UNWIND m)"


def _doc_inpath(q: TraversalQuery, schema: Schema, extra: tuple = ()) -> str:
    """An incoming hop: every source record whose list holds a link to the current
    record, one row per such link (`UNWIND`), which is the traversal's multiplicity.
    An edge projection reads the matching links' attribute instead of the source's
    field."""
    let = f"LET $s = {_doc_in_subquery(q, schema)}"
    tgt = q.source_table
    if q.projection in SET_PROJECTIONS:
        return f"SELECT id{_cols(extra)} FROM {tgt} {let}{_where(_sp(q), _set_filter('$s', q))}"
    proj, tail = _projected("$s", q, RENAMED, extra=extra)
    return f"SELECT id{_cols(extra)}, {proj} AS v FROM {tgt} {let}{_where(_sp(q))}{tail}"


# `unionall` takes an absent link list as an empty one
assumes("unionall.absent_operand.empty", SQL, True)


def _doc_union(q: TraversalQuery, schema: Schema, extra: tuple = ()) -> str:
    """The `both` direction: the outgoing path over the record's own list, unioned
    with the incoming subquery (`unionall` tolerates an absent list, probed)."""
    out_expr = _doc_expr(q.hops, q.edge_projection)
    if q.edge_projection:
        col = q.field
    else:
        col = q.field if q.projection in FIELD_PROJECTIONS else "id"
    let = f"LET $s = {_doc_in_subquery(q, schema)}"
    union = f"unionall({out_expr}.{col}, $s.v)"
    src = q.source_table
    if q.projection in SET_PROJECTIONS:
        return f"SELECT id{_cols(extra)} FROM {src} {let}{_where(_sp(q), _set_filter(union, q))}"
    proj, tail = _projected_flat(union, q, extra=extra)
    return f"SELECT id{_cols(extra)}, {proj} AS v FROM {src} {let}{_where(_sp(q))}{tail}"


# --- rel ------------------------------------------------------------------------------------


# LET precedes WHERE


# a nested LET's `$parent.$current` is the enclosing subquery's row, and `$f[0].v`
# keeps the projection flat (an aggregate over a list of lists is O5)
assumes("let.nested.parent_is_enclosing_query", SQL, True)
assumes("equivalent.let_first_element", SQL, True)


def _rel_sub(q: TraversalQuery, schema: Schema, k: int, cur: Path, direction: "str | None" = None) -> str:
    """The correlated subquery answering hops `k..` from the record `cur` names (the
    outer record's `id`, then the enclosing junction row's endpoint column).
    `direction` overrides the hop's (one of the two parts of a `both` hop)."""
    hop = q.hops[k]
    direction = direction or hop.direction
    out = direction == "out"
    near, far = ("src_id", "dst_id") if out else ("dst_id", "src_id")
    et = schema.edge_type(hop.edge_type)
    target = Lookup(et.to_type if out else et.from_type, Path(CURRENT, far).id, k)
    lets, where = [], [f"{near} = {cur.id}"]
    if hop.edge_predicate:
        where.append(render_pred(hop.edge_predicate))
    if hop.target_predicate:
        text, binds = condition(hop.target_predicate, target)
        lets += binds
        where.append(text)
    last = k == len(q.hops) - 1
    if not last:
        lets.append(f"$h{k + 1} = {_rel_sub(q, schema, k + 1, Path(CURRENT, far))}")
        select = f"$h{k + 1}.v AS v"
    elif q.edge_projection:
        select = f"{q.field} AS v"
    elif q.projection in FIELD_PROJECTIONS:
        text, binds = target.value(q.field)
        lets += binds
        select = f"{text} AS v"
    else:
        select = f"{far} AS v"
    let = f" LET {', '.join(lets)}" if lets else ""
    return f"(SELECT {select} FROM {hop.edge_type}{let}{_where(*where)})"


def _rel_let(q: TraversalQuery, schema: Schema, extra: tuple = ()) -> str:
    src = q.source_table
    if q.hops[0].direction == "both":
        let = f"LET $h0 = {_rel_sub(q, schema, 0, Path(CURRENT), 'out')}, $g0 = {_rel_sub(q, schema, 0, Path(CURRENT), 'in')}"
        coll = "unionall($h0.v, $g0.v)"
        if q.projection in SET_PROJECTIONS:
            return f"SELECT id{_cols(extra)} FROM {src} {let}{_where(_sp(q), _set_filter(coll, q))}"
        proj, tail = _projected_flat(coll, q, extra=extra)
        return f"SELECT id{_cols(extra)}, {proj} AS v FROM {src} {let}{_where(_sp(q))}{tail}"
    let = f"LET $h0 = {_rel_sub(q, schema, 0, Path(CURRENT))}"
    if q.projection in SET_PROJECTIONS:
        return f"SELECT id{_cols(extra)} FROM {src} {let}{_where(_sp(q), _set_filter('$h0', q))}"
    if q.projection == "count":
        proj, tail = "$h0.size()", ""
    else:
        proj, tail = _projected("$h0", q, RENAMED, extra=extra)
    return f"SELECT id{_cols(extra)}, {proj} AS v FROM {src} {let}{_where(_sp(q))}{tail}"


def _rel_flat_conds(q: TraversalQuery, schema: Schema, out: bool) -> "tuple[str, str, str, list]":
    h = q.hops[0]
    near, far = ("src_id", "dst_id") if out else ("dst_id", "src_id")
    et = schema.edge_type(h.edge_type)
    tgt = et.to_type if out else et.from_type
    where = []
    if q.source_predicate:
        where.append(condition(q.source_predicate, Semijoin(near, q.source_table))[0])
    if h.edge_predicate:
        where.append(render_pred(h.edge_predicate))
    if h.target_predicate:
        where.append(condition(h.target_predicate, Semijoin(far, tgt))[0])
    return near, far, tgt, where


def _rel_flat_part(q: TraversalQuery, schema: Schema, out: bool) -> str:
    h = q.hops[0]
    near, far, tgt, where = _rel_flat_conds(q, schema, out)
    lets = []
    if len(q.hops) > 1:
        lets.append(f"$h1 = {_rel_sub(q, schema, 1, Path(CURRENT, far))}")
        value = "$h1.v"
    elif q.edge_projection:
        value = q.field
    elif q.projection in FIELD_PROJECTIONS:
        value, binds = Lookup(tgt, Path(CURRENT, far).id).value(q.field)
        lets += binds
    else:
        value = far
    let = f" LET {', '.join(lets)}" if lets else ""
    if q.projection == "exists":
        return f"SELECT DISTINCT {near} AS id FROM {h.edge_type}{let}{_where(*where)}"
    if q.projection in AGG_PROJECTIONS or q.projection in EDGE_AGG_PROJECTIONS:
        kind = q.projection.lstrip("e") if q.edge_projection else q.projection
        return f"SELECT {near} AS id, {kind}({value}) AS v FROM {h.edge_type}{let}{_where(*where)} GROUP BY {near}"
    distinct = " DISTINCT" if q.projection == "distinct_ids" else ""
    return f"SELECT{distinct} {near} AS id, {value} AS v FROM {h.edge_type}{let}{_where(*where)}"


def _rel_flat(q: TraversalQuery, schema: Schema, extra: tuple = ()) -> str:
    h = q.hops[0]
    if q.projection == "nexists":
        # the anti-join: the start records absent from the junction's near column
        # (never index-served, so it sits next to an index-served source predicate)
        parts = []
        for out in ((True, False) if h.direction == "both" else (h.direction == "out",)):
            near, far, tgt, where = _rel_flat_conds(dataclasses.replace(q, source_predicate=None), schema, out)
            parts.append(f"id NOT IN (SELECT {near} FROM {h.edge_type}{_where(*where)})")
        return f"SELECT id{_cols(extra)} FROM {q.source_table}{_where(_sp(q), *parts)}"
    if h.direction != "both":
        return _rel_flat_part(q, schema, h.direction == "out")
    a = _rel_flat_part(q, schema, True)
    b = _rel_flat_part(q, schema, False)
    union = f"SELECT expand(unionall(({a}), ({b})))"
    if q.projection == "exists":
        return f"SELECT DISTINCT id FROM ({union})"
    return union


# --- dispatch ---------------------------------------------------------------------------------

_RENDERERS = {
    ("graph", "chain"): lambda q, s, x: _graph_chain(q, s, False, x),
    ("graph", "bag"): lambda q, s, x: _graph_chain(q, s, True, x),
    ("graph", "match"): _graph_match,
    ("graph", "match_opt"): lambda q, s, x: _graph_match(q, s, x, optional=True),
    ("graph", "edgescan"): _graph_edgescan,
    ("graph", "traverse"): _graph_traverse,
    ("doc", "path"): _doc_path,
    ("doc", "unwind"): _doc_unwind,
    ("doc", "let_expand"): _doc_let_expand,
    ("doc", "inpath"): _doc_inpath,
    ("doc", "union"): _doc_union,
    ("rel", "let"): _rel_let,
    ("rel", "flat"): _rel_flat,
}


def _reach_renderer(data_model: str, syntax: str):
    if data_model == "graph":
        return lambda q, s, x: _graph_reach(q, syntax, x)
    if data_model == "doc":
        return (lambda q, s, x: _doc_reach_traverse(q, x)) if syntax == "traverse" else (lambda q, s, x: _doc_reach(q, x))
    return lambda q, s, x: _rel_reach(q, x)


# the pull-up asks the correlated syntax inside a scanned FROM subquery (O1)
assumes("equivalent.pulled_up", SQL, True)


def render(q: TraversalQuery, schema: Schema, data_model: str, syntax: str, pull_up: bool = False) -> str:
    try:
        fn = _reach_renderer(data_model, syntax) if q.projection in REACH_PROJECTIONS else _RENDERERS[(data_model, syntax)]
    except KeyError:
        raise ValueError((data_model, syntax)) from None
    if not pull_up:
        return fn(q, schema, ())
    if not can_pull_up(q):
        raise ValueError("cannot pull up")
    inner_q = dataclasses.replace(q, source_predicate=None)
    if data_model == "rel" and syntax == "flat" and q.projection != "nexists":
        inner = fn(inner_q, schema, ())
        cond = condition(q.source_predicate, Semijoin("id", q.source_table))[0]
    else:
        inner = fn(inner_q, schema, tuple(fields_of(q.source_predicate)))
        cond = _sp(q)
    cols = "id" if q.projection in SET_PROJECTIONS else "id, v"
    return f"SELECT {cols} FROM ({inner}) WHERE {cond}"

