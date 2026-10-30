from __future__ import annotations

import random
from dataclasses import dataclass, field

from ganda.core.data.model import FieldType, Instance, Schema
from ganda.core.query.ir.write import EDGE_OPS, WriteQuery
from ganda.surrealdb.concrete_syntax.query.predicate import present, render_predicate as render_pred, tables_of
from ganda.surrealdb.engine.dialect import engine_id, surql_literal
from ganda.surrealdb.rules import assumes
from ganda.surrealdb.rules.catalog import GRAPH, SURQL

TARGETS = ("table", "sub")
DOORS = ("auto", "tx")

assumes("delete.record.cascades_relations", GRAPH, True)
assumes("link.deleted_target.field_reads_none", SURQL, True)


@dataclass(frozen=True)
class WriteSyntax:
    target: str = "table"
    door: str = "auto"
    form: str = ""

    @property
    def tag(self) -> str:
        parts = [x for x, default in ((self.form, ""), (self.target, "table"), (self.door, "auto")) if x != default]
        return "+".join(parts) or "plain"


def spelling(sp: WriteSyntax) -> tuple:
    """What every data model's write shares: the target and the door."""
    return (sp.target, sp.door)


@dataclass
class WriteRewrites:
    """The per-case material of a write: none (its filter is the question's own)."""


@dataclass
class Statements:
    """What one data model sends: `prelude` statements (a count taken before a rewrite,
    the rel data model's cascade), the `main` expression whose value is compared (None:
    nothing is compared on this data model), `postlude` statements (the doc data
    model's cascade, after the delete), and the filter's bind variables."""

    prelude: list = field(default_factory=list)
    main: "str | None" = None
    door: str = "auto"
    postlude: list = field(default_factory=list)
    binds: dict = field(default_factory=dict)

    @property
    def params(self) -> dict:
        return dict(self.binds)

    def script(self) -> str:
        body = list(self.prelude)
        if self.main:
            body.append(f"LET $r = {self.main};")
        body += self.postlude
        if self.door == "tx":
            body = ["BEGIN TRANSACTION;"] + body + ["COMMIT TRANSACTION;"]
        if self.main:
            body.append("RETURN $r;")
        return "\n".join(body)

    def display(self) -> str:
        return self.script()


def forms(q: WriteQuery, data_model: str) -> tuple:
    return ("closure",) if q.op in EDGE_OPS and data_model == "doc" else ("",)


def all_syntaxes(q: WriteQuery, data_model: str) -> list:
    return [WriteSyntax(t, d, m) for m in forms(q, data_model) for t in TARGETS for d in DOORS]


def applicable(q: WriteQuery, sp: WriteSyntax, rewrites: WriteRewrites, data_model: str) -> bool:
    if q.op in EDGE_OPS and data_model == "doc":
        return sp.target == "table"
    if q.op == "upsert":
        return sp.target == "table"
    return True


# --- rendering -------------------------------------------------------------------------------


def _ftype(q: WriteQuery, schema: Schema) -> "FieldType | None":
    try:
        return schema.field(q.edge_type, q.field, edge=True).type if q.edge else schema.field(q.table, q.field).type
    except KeyError:
        return None


def _where(q: WriteQuery, schema: Schema, prefix: str = "", element: bool = False) -> "str | None":
    text = render_pred(q.where, prefix, element, schema) if q.where is not None else None
    if q.extra_notnull:
        guard = present(prefix + q.field)
        return f"({text}) AND {guard}" if text else guard
    return text


def _filter(q: WriteQuery, sp: WriteSyntax, schema: Schema, tables: str) -> str:
    where = _where(q, schema)
    if sp.target == "sub":
        return f" WHERE id IN (SELECT VALUE id FROM {tables}" + (f" WHERE {where}" if where else "") + ")"
    return f" WHERE {where}" if where else ""


def _change(q: WriteQuery, schema: Schema) -> str:
    op = q.op.removeprefix("edge_")
    f = q.field
    lit = surql_literal(q.value, _ftype(q, schema)) if op in ("set", "upsert", "merge", "arith") else None
    if op in ("set", "upsert"):
        return f"SET {f} = {lit}"
    if op == "arith":
        return f"SET {f} = {f} {q.sym} {lit}"
    if op == "null":
        return f"SET {f} = NULL"
    if op == "remove":
        return f"UNSET {f}"
    if op == "merge":
        return f"MERGE {{ {f}: {lit} }}"
    raise ValueError(q.op)


def _returning(q: WriteQuery) -> str:
    if q.returning == "count":
        return ""
    v = f"$before.{q.field}" if q.returning == "before" else q.field
    return f" RETURN id, {v} AS v"


def _counted(text: str) -> str:
    return f"{{ count: array::len(({text})) }}"


def _node_update(q: WriteQuery, sp: WriteSyntax, schema: Schema) -> str:
    tables = tables_of(schema, q.table)
    if q.op == "upsert":
        text = f"UPSERT {engine_id(q.target_id)} {_change(q, schema)}{_returning(q)}"
    else:
        text = f"UPDATE {tables} {_change(q, schema)}{_filter(q, sp, schema, tables)}{_returning(q)}"
    return f"({text})" if q.returning != "count" else _counted(text)


def _victims(q: WriteQuery, schema: Schema, tables: str) -> str:
    where = _where(q, schema)
    return f"(SELECT VALUE id FROM {tables}" + (f" WHERE {where}" if where else "") + ")"


def _node_delete(q: WriteQuery, sp: WriteSyntax, schema: Schema, data_model: str) -> Statements:
    tables = tables_of(schema, q.table)
    victim_tables = {nt.name for nt in schema.node_types} if schema.is_base(q.table) else {q.table}
    st = Statements(door=sp.door)
    if data_model == "rel":
        v = _victims(q, schema, tables)
        for et in schema.edge_types:
            if et.from_type in victim_tables or et.to_type in victim_tables:
                st.prelude.append(f"DELETE {et.name} WHERE src IN {v} OR dst IN {v};")
    elif data_model == "doc":
        for et in schema.edge_types:
            if et.to_type in victim_tables:
                st.postlude.append(f"UPDATE {et.from_type} SET {et.name} = ({et.name} ?? [])[WHERE target.id IS NOT NONE] WHERE array::len(({et.name} ?? [])[WHERE target.id IS NONE]) > 0;")
    st.main = _counted(f"DELETE {tables}{_filter(q, sp, schema, tables)} RETURN BEFORE")
    return st


def _edge_write(q: WriteQuery, sp: WriteSyntax, schema: Schema, data_model: str) -> Statements:
    et = schema.edge_type(q.edge_type)
    st = Statements(door=sp.door)
    if data_model == "doc":
        p = _where(q, schema, "$l.", element=True) or "true"
        arr = f"{et.name} ?? []"
        matching = f"array::filter({arr}, |$l| {p})"
        st.prelude.append(f"LET $c = math::sum((SELECT VALUE array::len({matching}) FROM {et.from_type}));")
        op = q.op.removeprefix("edge_")
        if op == "delete":
            new = f"array::filter({arr}, |$l| NOT ({p}))"
        else:
            f = q.field
            if op == "set":
                changed = f"$l + {{ {f}: {surql_literal(q.value, _ftype(q, schema))} }}"
            elif op == "arith":
                changed = f"$l + {{ {f}: $l.{f} {q.sym} {surql_literal(q.value, _ftype(q, schema))} }}"
            elif op == "remove":
                changed = f"object::remove($l, '{f}')"
            else:
                raise ValueError(q.op)
            new = f"array::map({arr}, |$l| IF {p} THEN {changed} ELSE $l END)"
        st.prelude.append(f"UPDATE {et.from_type} SET {et.name} = {new} WHERE array::len({matching}) > 0;")
        st.main = "{ count: $c }"
        return st
    cond = _filter(q, sp, schema, et.name)
    if q.op == "edge_delete":
        st.main = _counted(f"DELETE {et.name}{cond} RETURN BEFORE")
    else:
        st.main = _counted(f"UPDATE {et.name} {_change(q, schema)}{cond}")
    return st


def render(q: WriteQuery, sp: WriteSyntax, rewrites: WriteRewrites, data_model: str, before: Instance, after: Instance) -> Statements:
    schema = before.schema
    if q.op in EDGE_OPS:
        st = _edge_write(q, sp, schema, data_model)
    elif q.op == "delete":
        st = _node_delete(q, sp, schema, data_model)
    else:
        st = Statements(main=_node_update(q, sp, schema), door=sp.door)
    st.binds = dict(q.where.bind_vars) if q.where is not None else {}
    return st


def build_rewrites(q: WriteQuery, rng: random.Random, schema: Schema, instance: Instance) -> WriteRewrites:
    return WriteRewrites()


def execute(session, st: Statements) -> list:
    """Run one data model's statements; the compared expression's value."""
    if not st.prelude and not st.main and not st.postlude:
        return []
    return session.execute(st.script(), st.binds)


def shared_binds(st: Statements) -> dict:
    return st.params


def rewrite_params(rewrites: WriteRewrites) -> dict:
    return {}
