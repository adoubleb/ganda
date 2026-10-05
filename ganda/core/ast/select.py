"""The select AST: the single-class questions -- one class, an optional filter, a
result shape -- as data, so every shape is asked with and without a filter and through
every concrete syntax that applies (a provider's `select_syntax`).

A `SelectQuery` is a class (`table`, the base class included), a predicate tree
(`where`, None for the whole class) and a `kind`:

    kind      the question                                        comparison
    ids       the matching records' ids                           a set of rows
    count     how many match (0 over none)                        one value
    field     each match's field                                  by id
    method    each match's method expression over a present field by id
    agg       one aggregate over the matches (`AGG_FNS`)          one value (a bag for set/list)
    group     an aggregate per value of one or two keys           a set of rows
    distinct  the distinct values of one or two fields            a set of rows
    page      a page of an ordering made total by the id tiebreak the ordered list
    keys      the key rows in the order of one or two keys, no    the ordered list
              tiebreak (ties carry identical rows)
    join      per match, the records of the class whose `f`       by id
              compares to its own: their count or their ids

`fields` are the projected / aggregated / grouped / ordered / joined fields, `arg` a
group's aggregated field (a join's form, `count` or `ids`), `fn` the aggregate (the
method template, a join's operator), `directions` / `skip` / `limit` an ordering's.

Two more dimensions of the question:

- the TARGET: a node class, or with `edge` an EDGE TYPE -- the relationship records
  as each data model stores them (`eid` is their id);
- the TRANSACTION door (`tx`): the question read inside a transaction that first
  writes one record of the class (updated, inserted or deleted) and is then rolled
  back -- the answer is the Instance's with the write applied (`tx_instance`).

A filter may hold a SEMI-JOIN leaf (`f IN (SELECT g FROM t2 [WHERE q])`,
`predicates.draw_insub`); `hits` recomputes its answered values over the Instance it
evaluates (the transaction's included).

`evaluate` answers a query over the records the filter keeps (the reference leg); it
is the same answer for every concrete syntax. What an engine decides -- an aggregate
over no rows, where nulls sort, what counts as the same group key, a method's result --
comes from its `Semantics` and its method catalog.
"""
from __future__ import annotations

import dataclasses
import itertools
from dataclasses import dataclass

from ganda.core.model import FieldType, Instance, Schema
from ganda.core.ast import predicates
from ganda.core.case import Comparison
from ganda.core.ast.predicates import Compound, Predicate

KINDS = ("ids", "count", "field", "method", "agg", "group", "distinct", "page", "keys", "join")
# the kinds over an edge type (a join correlates a class with itself: nodes only)
EDGE_KINDS = ("ids", "count", "field", "method", "agg", "group", "distinct", "page", "keys")
# the kinds the doc data model renders over its embedded link documents
DOC_EDGE_KINDS = ("ids", "count")
JOIN_OPS = ("=", "<>", "<", "<=", ">", ">=")
JOIN_FORMS = ("count", "ids")
TX_WRITES = ("update", "update", "insert", "delete")
AGG_FNS = ("max", "min", "sum", "count", "avg", "median", "set", "list", "distinct_count")
# a group's aggregate: `count(*)`, or a function of `arg`
GROUP_FNS = ("count_star", "count", "max", "min", "sum")
DIRECTIONS = ("ASC", "DESC")


@dataclass(frozen=True)
class SelectQuery:
    table: str
    where: "Predicate | Compound | None"
    kind: str
    fields: tuple = ()
    fn: "str | None" = None
    arg: "str | None" = None
    directions: tuple = ()
    skip: int = 0
    limit: int = 0
    edge: bool = False  # `table` names an edge type
    tx: "dict | None" = None  # the transaction door's write (`tx_instance`)

    @property
    def field(self) -> "str | None":
        return self.fields[0] if self.fields else None

    @property
    def bind_vars(self) -> dict:
        return dict(self.where.bind_vars) if self.where is not None else {}

    @property
    def comparison(self) -> Comparison:
        if self.kind == "join":
            return Comparison.SCALAR_BY_ID if self.arg == "count" else Comparison.MULTISET_BY_ID
        if self.kind in ("field", "method"):
            return Comparison.SCALAR_BY_ID
        if self.kind in ("count", "page", "keys"):
            return Comparison.SINGLE_VALUE
        if self.kind == "agg":
            return Comparison.NESTED_VALUE if self.fn in ("set", "list") else Comparison.SINGLE_VALUE
        return Comparison.NESTED_VALUE

    def to_params(self) -> dict:
        return {
            "table": self.table,
            "where": self.where.to_dict() if self.where is not None else None,
            "kind": self.kind,
            "fields": list(self.fields),
            "fn": self.fn,
            "arg": self.arg,
            "directions": list(self.directions),
            "skip": self.skip,
            "limit": self.limit,
            "edge": self.edge,
            "tx": self.tx,
        }


def from_params(d: dict) -> SelectQuery:
    return SelectQuery(
        table=d["table"],
        where=Predicate.from_dict(d.get("where")),
        kind=d["kind"],
        fields=tuple(d.get("fields") or ()),
        fn=d.get("fn"),
        arg=d.get("arg"),
        directions=tuple(d.get("directions") or ()),
        skip=d.get("skip", 0),
        limit=d.get("limit", 0),
        edge=d.get("edge", False),
        tx=d.get("tx"),
    )


def column(schema: Schema, q: SelectQuery, field_name: str):
    """The `FieldSpec` of a field of the query's target (a node class's own and
    inherited fields, or an edge type's attributes)."""
    return schema.field(q.table, field_name, edge=q.edge)


def target_fields(schema: Schema, table: str, edge: bool) -> tuple:
    return schema.edge_type(table).fields if edge else schema.query_fields(table)


def records(q: SelectQuery, instance: Instance) -> list:
    return instance.edges_of(q.table) if q.edge else instance.nodes_of(q.table)


def tx_instance(instance: Instance, tx: dict) -> Instance:
    """The Instance as the transaction sees it after its write (`support.tx_rows`
    on the written class; every other record unchanged)."""
    from ganda.core.generators.support import tx_rows

    table = tx["table"]
    rows = tx_rows(instance, tx)
    others = [n for n in instance.nodes if n.type_name != table]
    return dataclasses.replace(instance, nodes=others + rows)


def view(q: SelectQuery, instance: Instance) -> Instance:
    """The Instance the question reads: the transaction's where it has one."""
    return tx_instance(instance, q.tx) if q.tx else instance


def notnull_fields(q: SelectQuery, form: str) -> tuple:
    """The fields a concrete syntax's filter must also hold present (`f IS NOT NULL`): a
    method projection's field, the counted field of a DISTINCT subquery, the ordered
    field of `max` / `min` read as the first row of an ordering."""
    if q.kind == "method":
        return (q.field,)
    if q.kind == "agg" and ((q.fn == "distinct_count" and form == "base") or form == "order"):
        return (q.field,)
    return ()


def effective(q: SelectQuery, form: str, pred) -> "Predicate | Compound | None":
    """The predicate a concrete syntax actually filters by: `pred` (the filter or one of its
    rewrites) with the concrete syntax's `IS NOT NULL` conjuncts -- what the guards judge."""
    extra = []
    for f in notnull_fields(q, form):
        extra.append(Predicate("source", f, "IS NOT NULL", "", None))
    parts = ([pred] if pred is not None else []) + extra
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else Compound("source", "AND", tuple(parts))


def hits(q: SelectQuery, instance: Instance) -> list:
    """The records the filter keeps, over the question's view of the Instance (the
    semi-joins' answered values recomputed over it)."""
    inst = view(q, instance)
    rows = records(q, inst)
    if q.where is None:
        return rows
    where = predicates.resolve_subs(q.where, inst) if predicates.subqueries(q.where) else q.where
    return [n for n in rows if predicates.evaluate(n.values, where, instance.schema.engine.semantics)]


def present_values(q: SelectQuery, instance: Instance, field_name: "str | None" = None) -> list:
    f = field_name or q.field
    return [n.values.get(f) for n in hits(q, instance) if n.values.get(f) is not None]


# --- shape coverage --------------------------------------------------------------------------


def _detail(q: SelectQuery) -> str:
    if q.kind == "agg":
        return q.fn
    if q.kind == "group":
        return f"{q.fn}/{len(q.fields)}"
    if q.kind in ("distinct", "keys"):
        return str(len(q.fields))
    if q.kind == "page":
        return q.directions[0]
    if q.kind == "join":
        return f"{q.fn}/{q.arg}"
    return ""


def coverage_signature(q: SelectQuery) -> str:
    """The structure alone -- kind, its detail, filtered or not, the target (a node
    class or an edge type), the transaction door -- counted against
    `COVERAGE_DENOMINATOR`."""
    d = _detail(q)
    return f"{q.kind}{':' + d if d else ''} where={'f' if q.where is not None else '-'}" + (" on=edge" if q.edge else "") + (" tx" if q.tx else "")


def all_coverage_signatures() -> list:
    details = {
        "agg": list(AGG_FNS),
        "group": [f"{fn}/{n}" for fn in GROUP_FNS for n in (1, 2)],
        "distinct": ["1", "2"],
        "keys": ["1", "2"],
        "page": list(DIRECTIONS),
        "join": [f"{op}/{form}" for op in JOIN_OPS for form in JOIN_FORMS],
    }
    out = []
    for kind in KINDS:
        for d, w in itertools.product(details.get(kind, [""]), ("-", "f")):
            base = f"{kind}{':' + d if d else ''} where={w}"
            out.append(base)
            if kind != "join":
                out.append(base + " tx")
            if kind in EDGE_KINDS:
                out.append(base + " on=edge")
    return out


COVERAGE_DENOMINATOR = len(all_coverage_signatures())


def shape_signature(q: SelectQuery, schema: Schema) -> str:
    """The census and deduplication key: the coverage signature refined by the filter's
    operators and the column types involved."""
    d = _detail(q)
    cols = list(q.fields) + ([q.arg] if q.arg and q.kind == "group" else [])
    types = "+".join(column(schema, q, f).type.value for f in cols) or "-"
    where = q.where.signature if q.where is not None else "-"
    wtypes = "+".join(sorted({column(schema, q, f).type.value for f in predicates.fields_of(q.where)})) or "-"
    target = " on=edge" if q.edge else ""
    tx = f" tx={q.tx['write']}" if q.tx else ""
    return f"select {q.kind}{':' + d if d else ''}{target}{tx} type={types} where={where} wtype={wtypes}"


# --- the reference leg -----------------------------------------------------------------------


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
    filter of its own) whose `f` compares to its own -- the pivot is the record's
    STORED value (never a converted parameter), so the column type plays no part;
    the collation does (a ci value join is case-insensitive, probed)."""
    predicate_hit = instance.schema.engine.semantics.predicate_hit
    f, op = q.field, q.fn
    ci = column(instance.schema, q, f).ci
    inner = records(q, view(q, instance))
    out = {}
    for n in rows:
        pivot = n.values.get(f)
        m = [x for x in inner if predicate_hit(f in x.values, x.values.get(f), op, pivot, None, ci)]
        out[n.record_id] = len(m) if q.arg == "count" else [x.record_id for x in m]
    return out


def evaluate(q: SelectQuery, instance: Instance):
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
    if q.kind == "method":
        fn = instance.schema.engine.select_syntax.method_fn(q.fn, column(instance.schema, q, q.field).type)
        return {n.record_id: fn(n.values[q.field]) for n in rows if n.values.get(q.field) is not None}
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


def reference_ok(q: SelectQuery, instance: Instance) -> bool:
    """Whether `evaluate` is exact for `q`: the filter's leaves are, and an id-tiebroken
    ordering is one the engine's semantics reproduce (`orders_exactly`: a collated
    string order or custom keys may not be)."""
    if q.where is not None and not q.where.reference:
        return False
    if q.kind == "page":
        ftype = column(instance.schema, q, q.field).type
        custom_keys = any(getattr(n, "custom_key", None) is not None for n in records(q, view(q, instance)))
        return instance.schema.engine.semantics.orders_exactly(ftype, custom_keys)
    return True
