"""The write AST: data changes that pick their records through the planner -- an
update, a delete or an upsert by predicate, and the relationship-attribute writes --
where every write a setup issues addresses one record by its id.

A `WriteQuery` is an operation on a class (`table`, a node class or the base class)
or on an edge type (`edge_type`), a predicate tree choosing the records (`where`,
None for all of them), the written field and its new value or delta, and what the
statement answers (`returning`):

    op           the write (node ops on `table`, edge ops on `edge_type`)
    set          every match gets the value
    arith        `f + d` / `f * k` over the matches whose `f` is not null
    null         an explicit null (the field stays defined)
    remove       the field absent
    merge        the value merged in from a document literal
    delete       the records and every incident relationship
    upsert       an existing record, or a new one, by id
    edge_set / edge_arith / edge_remove / edge_delete   the same on the relationships

    returning    count (every op) | before | after (the node update ops): the matched
                 records' `id` and the written field before / after the write

`apply` is the reference: the Instance with the write applied; `answer` the rows the
statement must return over the Instance before it (`[{"count": n}]`, or one `{id, v}`
row per matched record). A provider's `write_syntax` renders the statements, runs them
and adapts its engine's answer to that shape.
"""
from __future__ import annotations

import dataclasses
import itertools
from dataclasses import dataclass
from typing import Any

from ganda.core.model import Instance, NodeRow, Schema
from ganda.core.ast import predicates
from ganda.core.case import Comparison
from ganda.core.ast.predicates import Predicate

NODE_OPS = ("set", "arith", "null", "remove", "merge", "delete", "upsert")
EDGE_OPS = ("edge_set", "edge_arith", "edge_remove", "edge_delete")
OPS = NODE_OPS + EDGE_OPS
# the ops whose statement may carry `RETURN BEFORE|AFTER` (a node update of one field)
RETURNING_OPS = ("set", "arith", "null", "remove", "merge")
RETURNINGS = ("count", "before", "after")


@dataclass(frozen=True)
class WriteQuery:
    op: str
    table: str = ""  # a node op's class
    edge_type: str = ""  # an edge op's relationship type
    where: "Predicate | None" = None
    field: "str | None" = None
    value: Any = None  # set / merge / upsert: the value; arith: the delta or factor
    sym: str = "+"  # arith: `+` or `*`
    returning: str = "count"
    target_id: "str | None" = None  # upsert: the record id (existing or new)
    new_key: "str | None" = None  # upsert of a new record: its custom key

    @property
    def edge(self) -> bool:
        return self.op in EDGE_OPS

    @property
    def target(self) -> str:
        return self.edge_type if self.edge else self.table

    @property
    def extra_notnull(self) -> bool:
        """An arithmetic write filters its null rows out (what `null + d` is differs per
        engine)."""
        return self.op in ("arith", "edge_arith")

    def to_params(self) -> dict:
        return {
            "op": self.op,
            "table": self.table,
            "edge_type": self.edge_type,
            "where": self.where.to_dict() if self.where is not None else None,
            "field": self.field,
            "value": self.value,
            "sym": self.sym,
            "returning": self.returning,
            "target_id": self.target_id,
            "new_key": self.new_key,
        }


def from_params(d: dict) -> WriteQuery:
    return WriteQuery(
        op=d["op"],
        table=d.get("table", ""),
        edge_type=d.get("edge_type", ""),
        where=Predicate.from_dict(d.get("where")),
        field=d.get("field"),
        value=d.get("value"),
        sym=d.get("sym", "+"),
        returning=d.get("returning", "count"),
        target_id=d.get("target_id"),
        new_key=d.get("new_key"),
    )


def comparison(q: WriteQuery) -> Comparison:
    """A count is one value; returned records are a bag of rows (a record returned
    twice is a finding)."""
    return Comparison.SINGLE_VALUE if q.returning == "count" else Comparison.NESTED_VALUE


# --- the reference leg -----------------------------------------------------------------------


def _match(values: dict, q: WriteQuery, sem) -> bool:
    if q.extra_notnull and values.get(q.field) is None:
        return False
    return q.where is None or predicates.evaluate(values, q.where, sem)


def node_hits(q: WriteQuery, instance: Instance) -> list:
    if q.op == "upsert":
        return [n for n in instance.nodes_of(q.table) if n.record_id == q.target_id]
    return [n for n in instance.nodes_of(q.table) if _match(n.values, q, instance.schema.engine.semantics)]


def edge_hits(q: WriteQuery, instance: Instance) -> list:
    return [e for e in instance.edges_of(q.edge_type) if _match(e.values, q, instance.schema.engine.semantics)]


def _field_type(q: WriteQuery, schema: Schema) -> str:
    if q.edge:
        return schema.field(q.edge_type, q.field, edge=True).type.value
    return schema.field(q.table, q.field).type.value


def _new_values(q: WriteQuery, values: dict, ftype: "str | None", sem) -> dict:
    out = dict(values)
    op = q.op.removeprefix("edge_")
    if op in ("set", "merge", "upsert"):
        out[q.field] = q.value
    elif op == "arith":
        out[q.field] = sem.apply_arith(values[q.field], q.sym, q.value, ftype)
    elif op == "null":
        out[q.field] = None
    elif op == "remove":
        out.pop(q.field, None)
    return out


def new_node(q: WriteQuery, instance: Instance) -> NodeRow:
    """The record an UPSERT inserts: the id and the SET field, nothing else."""
    local = max((n.local_id for n in instance.nodes), default=0) + 1000
    return NodeRow(q.table, local, {q.field: q.value}, custom_key=q.new_key)


def apply(q: WriteQuery, instance: Instance) -> Instance:
    """The Instance with the write applied (a new object; `instance` is untouched)."""
    schema = instance.schema
    if q.edge:
        hit = {e.record_id for e in edge_hits(q, instance)}
        ftype = _field_type(q, schema) if q.field else None
        edges = []
        for e in instance.edges:
            if e.record_id not in hit:
                edges.append(e)
            elif q.op != "edge_delete":
                edges.append(dataclasses.replace(e, values=_new_values(q, e.values, ftype, instance.schema.engine.semantics)))
        return dataclasses.replace(instance, edges=edges)
    hits = node_hits(q, instance)
    if q.op == "upsert" and not hits:
        return dataclasses.replace(instance, nodes=list(instance.nodes) + [new_node(q, instance)])
    hit = {n.record_id for n in hits}
    if q.op == "delete":
        nodes = [n for n in instance.nodes if n.record_id not in hit]
        edges = [e for e in instance.edges if e.from_record not in hit and e.to_record not in hit]
        return dataclasses.replace(instance, nodes=nodes, edges=edges)
    ftype = _field_type(q, schema)
    nodes = [dataclasses.replace(n, values=_new_values(q, n.values, ftype, instance.schema.engine.semantics)) if n.record_id in hit else n for n in instance.nodes]
    return dataclasses.replace(instance, nodes=nodes)


def answer(q: WriteQuery, instance: Instance) -> list:
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


def touched(q: WriteQuery, instance: Instance) -> "tuple[set, set]":
    """(node classes, edge types) whose stored state the write may change -- what
    the state check after it reads back."""
    schema = instance.schema
    if q.edge:
        return set(), {q.edge_type}
    tables = {nt.name for nt in schema.node_types} if schema.is_base(q.table) else {q.table}
    edges: set = set()
    if q.op == "delete":
        edges = {et.name for et in schema.edge_types if et.from_type in tables or et.to_type in tables}
        # the doc data model rewrites the link lists of every source pointing at a victim
        tables |= {et.from_type for et in schema.edge_types if et.name in edges}
    return tables, edges


# --- shape coverage --------------------------------------------------------------------------


def coverage_signature(q: WriteQuery) -> str:
    where = "f" if q.where is not None else "-"
    if q.op == "upsert":
        return f"upsert {'new' if q.new_key else 'existing'}"
    if q.op in RETURNING_OPS:
        return f"{q.op} where={where} ret={q.returning}"
    return f"{q.op} where={where}"


def all_coverage_signatures() -> list:
    out = []
    for op in NODE_OPS + EDGE_OPS:
        if op == "upsert":
            out += ["upsert existing", "upsert new"]
        elif op in RETURNING_OPS:
            out += [f"{op} where={w} ret={r}" for w, r in itertools.product(("-", "f"), RETURNINGS)]
        else:
            out += [f"{op} where={w}" for w in ("-", "f")]
    return out


COVERAGE_DENOMINATOR = len(all_coverage_signatures())


def shape_signature(q: WriteQuery, schema: Schema) -> str:
    ftype = _field_type(q, schema) if q.field else "-"
    where = q.where.signature if q.where is not None else "-"
    extra = f" sym={q.sym}" if q.op.endswith("arith") else ""
    return f"write {coverage_signature(q)} type={ftype}{extra} pred={where}"
