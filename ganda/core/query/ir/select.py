"""The select IR: the single-class questions -- one class, an optional filter, a
result shape -- as data, so every shape is asked with and without a filter and through
every concrete syntax that applies (a provider's `select_syntax`).
"""
from __future__ import annotations

import dataclasses
import itertools
from dataclasses import dataclass

from ganda.core.data.model import FieldType, Instance, Schema
from ganda.core.query.ir import predicates
from ganda.core.query.ir.enums import AggFn, GroupFn, JoinForm, JoinOp, SelectKind, SortDirection, WriteOp
from ganda.core.oracle.case import Comparison
from ganda.core.query.ir.predicates import Compound, Predicate

EDGE_KINDS = tuple(k for k in SelectKind if k is not SelectKind.JOIN)

DOC_EDGE_KINDS = (SelectKind.IDS, SelectKind.COUNT)

TX_WRITES = (WriteOp.UPDATE, WriteOp.UPDATE, WriteOp.INSERT, WriteOp.DELETE)


@dataclass(frozen=True)
class TxWrite:
    table: str
    write: WriteOp
    field: str
    tid: str
    tv: object = None
    remove: bool = False

    def __post_init__(self):
        object.__setattr__(self, "write", WriteOp(self.write))


@dataclass(frozen=True)
class SelectQuery:
    table: str
    where: "Predicate | Compound | None"
    kind: SelectKind
    fields: "tuple[str, ...]" = ()
    fn: "AggFn | GroupFn | JoinOp | str | None" = None
    arg: "JoinForm | str | None" = None
    directions: "tuple[SortDirection, ...]" = ()
    skip: int = 0
    limit: int = 0
    edge: bool = False  # `table` names an edge type
    tx: "TxWrite | None" = None  # the transaction door's write (`tx_instance`)

    def __post_init__(self):
        # coerce the plain strings `from_params` and the generators pass
        kind = SelectKind(self.kind)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "fields", tuple(self.fields))
        object.__setattr__(self, "directions", tuple(SortDirection(d) for d in self.directions))
        if self.fn is not None and kind in _FN_TYPES:
            object.__setattr__(self, "fn", _FN_TYPES[kind](self.fn))
        if self.arg is not None and kind is SelectKind.JOIN:
            object.__setattr__(self, "arg", JoinForm(self.arg))
        if isinstance(self.tx, dict):
            object.__setattr__(self, "tx", TxWrite(**self.tx))

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
            "tx": dataclasses.asdict(self.tx) if self.tx else None,
        }


# the enum a kind's `fn` holds (a method's is a free template)
_FN_TYPES = {SelectKind.AGG: AggFn, SelectKind.GROUP: GroupFn, SelectKind.JOIN: JoinOp}


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


def tx_instance(instance: Instance, tx: TxWrite) -> Instance:
    """The Instance as the transaction sees it after its write (`support.tx_rows`
    on the written class; every other record unchanged)."""
    from ganda.core.query.generators.support import tx_rows

    table = tx.table
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
        "agg": list(AggFn),
        "group": [f"{fn}/{n}" for fn in GroupFn for n in (1, 2)],
        "distinct": ["1", "2"],
        "keys": ["1", "2"],
        "page": list(SortDirection),
        "join": [f"{op}/{form}" for op in JoinOp for form in JoinForm],
    }
    out = []
    for kind in SelectKind:
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
    tx = f" tx={q.tx.write}" if q.tx else ""
    return f"select {q.kind}{':' + d if d else ''}{target}{tx} type={types} where={where} wtype={wtypes}"
