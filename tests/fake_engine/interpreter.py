"""The fake engine's query processor: how its in-memory "databases" answer a query,
by walking their own copy of the `Instance`. This is the double's executor, not an
oracle: the core compares what these sessions answer with each other, exactly as it
compares a real engine's data models.
"""
from __future__ import annotations

from typing import Any

from ganda.core.data.model import Instance
from ganda.core.query.ir.path import PathQuery, distance
from ganda.core.query.ir.select import SelectQuery, column, hits, records, view
from ganda.core.query.ir.traversal import AGG_PROJECTIONS, EDGE_AGG_PROJECTIONS, REACH_PROJECTIONS, SET_PROJECTIONS, TraversalQuery, _hit, _projected_values, final_paths, projected_type, reach
from ganda.core.query.ir.write import WriteQuery, _field_type, _new_values, edge_hits, node_hits


# --- traversals -------------------------------------------------------------------------------


def evaluate_traversal(q: TraversalQuery, instance: Instance) -> Any:
    """{start id: value} for row-keyed projections; a sorted id list for `exists` /
    `nexists`."""
    out: dict = {}
    bare: list = []
    if q.projection in REACH_PROJECTIONS:
        for n in instance.nodes_of(q.source_table):
            if q.source_predicate is not None and not _hit(instance, n.values, q.source_predicate):
                continue
            r = reach(q, instance, n.record_id)
            out[n.record_id] = r if q.projection.endswith(("2", "3")) else len(r)
        return out
    ftype = projected_type(q, instance) if q.projection in AGG_PROJECTIONS or q.projection in EDGE_AGG_PROJECTIONS else None
    for n in instance.nodes_of(q.source_table):
        if q.source_predicate is not None and not _hit(instance, n.values, q.source_predicate):
            continue
        paths = final_paths(q, instance, n.record_id)
        ids = [t for t, _ in paths]
        if q.projection in SET_PROJECTIONS:
            if bool(ids) == (q.projection == "exists"):
                bare.append(n.record_id)
            continue
        if q.projection == "ids":
            v: Any = ids
        elif q.projection == "distinct_ids":
            v = sorted(set(ids))
        elif q.projection == "count":
            v = len(ids)
        elif q.projection == "distinct_count":
            v = len(set(ids))
        else:
            values = _projected_values(q, instance, paths)
            v = values if q.projection in ("field", "efield") else instance.schema.engine.semantics.agg(q.projection.lstrip("e") if q.edge_projection else q.projection, values, ftype)
        out[n.record_id] = v
    if q.projection in SET_PROJECTIONS:
        return sorted(bare)
    return out



# --- selects ----------------------------------------------------------------------------------


def _agg(q: SelectQuery, instance: Instance, rows: list) -> list:
    sem = instance.schema.engine.semantics
    value_key, mean, agg = sem.key, sem.mean, sem.agg
    fn, f = q.fn, q.field
    values = [n.values.get(f) for n in rows]
    present = [v for v in values if v is not None]
    if fn == "count":
        return [{"v": len(present)}]
    if not rows:
        return sem.aggregate_over_no_rows(fn)
    if fn == "distinct_count":
        return [{"v": len({value_key(v) for v in present})}]
    if fn == "avg":
        return [{"v": mean(present)}] if present else [{"v": None}]
    if fn == "median":
        if not present:
            return [{"v": None}]
        ordered = sorted(present)
        m = len(ordered) // 2
        return [{"v": ordered[m] if len(ordered) % 2 else (ordered[m - 1] + ordered[m]) / 2}]
    if fn == "set":
        seen: dict = {}
        for v in present:
            seen.setdefault(value_key(v), v)
        return [{"v": v} for v in seen.values()]
    if fn == "list":
        return [{"v": v} for v in present]
    return [{"v": agg(fn, values, column(instance.schema, q, f).type.value)}]


def _group(q: SelectQuery, instance: Instance, rows: list) -> list:
    sem = instance.schema.engine.semantics
    value_key, agg = sem.key, sem.agg
    groups: dict = {}
    for n in rows:
        keys = tuple(n.values.get(k) for k in q.fields)
        groups.setdefault(tuple(value_key(k) for k in keys), [keys, []])[1].append(n)
    ftype = column(instance.schema, q, q.arg).type.value if q.arg else None
    out = []
    for keys, members in groups.values():
        row = {"k": keys[0]}
        if len(keys) > 1:
            row["k2"] = keys[1]
        if q.fn == "count_star":
            row["v"] = len(members)
        else:
            values = [m.values.get(q.arg) for m in members]
            row["v"] = len([v for v in values if v is not None]) if q.fn == "count" else agg(q.fn, values, ftype)
        out.append(row)
    return out


def _page(q: SelectQuery, rows: list, sem) -> list:
    value_key = sem.key
    f, desc = q.field, q.directions[0] == "DESC"
    present = [n for n in rows if n.values.get(f) is not None]
    absent = [n for n in rows if n.values.get(f) is None]
    present.sort(key=lambda n: (value_key(n.values[f]), value_key(n.record_id)))
    if desc:
        # a DESC order reverses the key order but keeps the id tiebreak ascending
        present.sort(key=lambda n: value_key(n.record_id))
        present.sort(key=lambda n: value_key(n.values[f]), reverse=True)
    absent.sort(key=lambda n: value_key(n.record_id))
    ordered = (absent + present) if sem.nulls_first(desc) else (present + absent)
    return [{"id": n.record_id} for n in ordered[q.skip: q.skip + q.limit]]


def _keys(q: SelectQuery, rows: list, sem) -> list:
    ordered = sorted(rows, key=lambda n: tuple(sem.order_key(n.values.get(k), d == "DESC") for k, d in zip(q.fields, q.directions)))
    page = ordered[q.skip:]
    if q.limit:
        page = page[: q.limit]
    return [{"ab"[i]: n.values.get(k) for i, k in enumerate(q.fields)} for n in page]


def _join(q: SelectQuery, instance: Instance, rows: list) -> dict:
    """Per kept record, the records of the class (every one: the LET subquery has no
    filter of its own) whose `f` compares to its own -- the constant is the record's
    STORED value (never a converted parameter), so the column type plays no part;
    the collation does (a ci value join is case-insensitive, probed)."""
    predicate_hit = instance.schema.engine.semantics.predicate_hit
    f, op = q.field, q.fn
    ci = column(instance.schema, q, f).case_insensitive
    inner = records(q, view(q, instance))
    out = {}
    for n in rows:
        constant = n.values.get(f)
        m = [x for x in inner if predicate_hit(f in x.values, x.values.get(f), op, constant, None, ci)]
        out[n.record_id] = len(m) if q.arg == "count" else [x.record_id for x in m]
    return out


def evaluate_select(q: SelectQuery, instance: Instance):
    """The answer every concrete syntax of `q` must give: a row list for the bare
    comparisons, {id: value} for the by-id ones."""
    rows = hits(q, instance)
    if q.kind == "join":
        return _join(q, instance, rows)
    if q.kind == "ids":
        return [{"id": n.record_id} for n in rows]
    if q.kind == "count":
        return [{"v": len(rows)}]
    if q.kind == "field":
        return {n.record_id: n.values.get(q.field) for n in rows}
    if q.kind == "agg":
        return _agg(q, instance, rows)
    if q.kind == "group":
        return _group(q, instance, rows)
    if q.kind == "distinct":
        key = instance.schema.engine.semantics.key
        seen: dict = {}
        for n in rows:
            vals = tuple(n.values.get(f) for f in q.fields)
            seen.setdefault(tuple(key(v) for v in vals), vals)
        return [dict(zip(("v", "w"), vals)) for vals in seen.values()]
    if q.kind == "page":
        return _page(q, rows, instance.schema.engine.semantics)
    if q.kind == "keys":
        return _keys(q, rows, instance.schema.engine.semantics)
    raise ValueError(q.kind)




# --- paths ------------------------------------------------------------------------------------


def evaluate_path(q: PathQuery, instance: Instance) -> list:
    d = distance(q, instance)
    return [{"v": d + 1}] if d is not None and d <= q.depth else []


# --- writes -----------------------------------------------------------------------------------


def write_answer(q: WriteQuery, instance: Instance) -> list:
    """The rows the statement returns, over the Instance BEFORE the write."""
    if q.edge:
        return [{"count": len(edge_hits(q, instance))}]
    if q.op == "upsert":
        return [{"count": 1}]
    hits = node_hits(q, instance)
    if q.returning == "count" or q.op == "delete":
        return [{"count": len(hits)}]
    ftype = _field_type(q, instance.schema)
    if q.returning == "before":
        return [{"id": n.record_id, "v": n.values.get(q.field)} for n in hits]
    return [{"id": n.record_id, "v": _new_values(q, n.values, ftype, instance.schema.engine.semantics).get(q.field)} for n in hits]


