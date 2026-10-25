"""The concrete syntax catalog of the WRITE IR: HOW each data model issues a `WriteQuery`. A write
runs ONCE per database, so a case does not carry variants: the data models share the
target and the door, each with its own form, and the answers and the resulting states
are compared with each other. Every data model's statement states the filter, so the
engine chooses the records on each (the Instance with the write applied steers later
draws only). The choices:

    axis     choice       the text
    target   table        `UPDATE t ... WHERE p`
             nest         `UPDATE t ... WHERE id IN (SELECT id FROM t WHERE p)`: the
                          records chosen before any is written
             sub          `UPDATE (SELECT FROM t WHERE p) ...`, a lazily streamed
                          subquery target (`DELETE VERTEX FROM (SELECT ...)` on the
                          graph data model)
    door     auto         auto-commit commands
             tx           `BEGIN; ...; LET $r = <write>; COMMIT; RETURN $r`
    form     update_edge  the graph data model's `UPDATE EDGE e ...` (else `UPDATE e ...`)
             bracket      the doc data model's edge delete as `e = e[NOT (p)]`
             position     the doc data model's edge-attribute write, one statement per
                          list position k: `UPDATE src SET e[k].f = ... WHERE e.size() > k
                          AND p(e[k])` (a filtered `e[p].f = ...` answers its count and
                          changes nothing, probed)

Per model: a node delete is `DELETE FROM t` (doc, rel) or `DELETE VERTEX t` (graph:
`DELETE FROM` a vertex or edge class is refused, probed), and its cascade is the
engine's own on the graph data model, `DELETE FROM e WHERE src_id IN (SELECT id FROM t
WHERE p) OR dst_id IN (...)` first on the rel data_model, and on the doc data model
`e = e[target.id IS NOT NULL]` on every source after the delete (a LINK to a deleted
record dangles and `target.id` is null). An edge write is `UPDATE EDGE` / `DELETE
EDGE` on the E class and a plain `UPDATE` / `DELETE FROM` of the junction; on the doc
data model its answer is not the relationship count (a count per position), so an
edge case compares the graph and rel answers and checks all three states (`partial`).

Every UPDATE carries `TIMEOUT <ms> EXCEPTION` (an update through an index walk can
revisit the records it moves and never finish; `DELETE` takes no TIMEOUT clause, probed).
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field

from ganda.core.data.model import Instance, Schema
from ganda.orientdb.concrete_syntax.predicate import render_predicate as render_pred
from ganda.core.data.model import FieldType
from ganda.core.query.ir.write import EDGE_OPS, WriteQuery

from ganda.orientdb.rules import assumes, works_around
from ganda.orientdb.rules.catalog import SQL

# a write's filter is the drawn predicate's own text on every data model
TARGETS = ("table", "nest", "sub")
DOORS = ("auto", "tx")
WRITE_TIMEOUT_MS = 10000
_T = f"TIMEOUT {WRITE_TIMEOUT_MS} EXCEPTION"
# the ops whose statement is an UPDATE of one node field (a `sub` target applies)
_NODE_UPDATES = ("set", "arith", "null", "remove", "merge")


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
    """What every data model's write shares: the target and the door (the form is the
    data model's own)."""
    return (sp.target, sp.door)


@dataclass
class WriteRewrites:
    """The per-case material of a write: none (its filter is the question's own)."""


@dataclass
class Statements:
    """What one side sends: `prelude` commands (the rel data model's cascade), the doc
    data model's per-position update (`positions`: the statement for position `{k}`, run
    for every position the database's longest list has), the `main` statement whose
    answer is compared (None: nothing is compared on this data model) and `postlude`
    commands (the doc data model's cascade, after the delete)."""

    prelude: list = field(default_factory=list)  # [(text, params)]
    main: "tuple | None" = None
    door: str = "auto"
    postlude: list = field(default_factory=list)
    positions: "tuple | None" = None  # (the longest list's query, (text, params))

    def at_positions(self, n: int) -> "Statements":
        """The statements with the per-position update run at positions 0..n-1."""
        if self.positions is None:
            return self
        text, params = self.positions[1]
        each = [(text.replace("{k}", str(k)), params) for k in range(n)]
        return Statements(self.prelude + each, self.main, self.door, self.postlude)

    @property
    def params(self) -> dict:
        out: dict = {}
        for _t, p in self.prelude + ([self.main] if self.main else []) + self.postlude:
            out.update(p)
        if self.positions is not None:
            out.update(self.positions[1][1])
        return out

    def script(self) -> str:
        body = [t for t, _p in self.prelude]
        if self.main:
            body.append(f"LET $r = {self.main[0]}")
        body += [t for t, _p in self.postlude]
        text = "BEGIN; " + "; ".join(body) + "; COMMIT;"
        return text + (" RETURN $r" if self.main else "")

    def display(self) -> str:
        if self.positions is not None:
            size, (text, _p) = self.positions
            return f"for k below ({size}): {text}" + (" (in one transaction)" if self.door == "tx" else "")
        if self.door == "tx":
            return self.script()
        return "; ".join([t for t, _p in self.prelude] + ([self.main[0]] if self.main else []) + [t for t, _p in self.postlude])


def forms(q: WriteQuery, data_model: str) -> tuple:
    if q.op in EDGE_OPS and data_model == "doc":
        return ("bracket",) if q.op == "edge_delete" else ("position",)
    if q.op in EDGE_OPS and data_model == "graph" and q.op != "edge_delete":
        return ("update_edge", "update")
    return ("",)


def all_syntaxes(q: WriteQuery, data_model: str) -> list:
    return [WriteSyntax(t, d, m) for m in forms(q, data_model) for t in TARGETS for d in DOORS]


def applicable(q: WriteQuery, sp: WriteSyntax, rewrites: WriteRewrites, data_model: str) -> bool:
    doc_edge = q.op in EDGE_OPS and data_model == "doc"
    if doc_edge:
        if sp.target != "table":
            return False
        if sp.form == "bracket":
            # the predicate itself in a bracket filter
            return q.where is not None
        return True
    if q.op == "upsert":
        return sp.target == "table"
    if sp.target == "nest" and q.where is None and not q.extra_notnull:
        return False
    if sp.target == "sub":
        return q.op in _NODE_UPDATES or (q.op == "delete" and data_model == "graph")
    return True


# --- rendering -------------------------------------------------------------------------------


def _where(q: WriteQuery, sp: WriteSyntax, rewrites: WriteRewrites) -> "str | None":
    text = render_pred(q.where) if q.where is not None else None
    if q.extra_notnull:
        return f"({text}) AND {q.field} IS NOT NULL" if text else f"{q.field} IS NOT NULL"
    return text


# UPDATE ... CONTENT and MERGE take a JSON literal, INCREMENT is refused


def _change(q: WriteQuery, params: dict) -> str:
    op = q.op.removeprefix("edge_")
    f = q.field
    if op in ("set", "upsert"):
        params["wv"] = q.value
        return f"SET {f} = :wv"
    if op == "arith":
        params["wd"] = q.value
        return f"SET {f} = {f} {q.sym} :wd"
    if op == "null":
        return f"SET {f} = null"
    if op == "remove":
        return f"REMOVE {f}"
    if op == "merge":
        # MERGE takes no bind parameter: a JSON literal, never SQL-interpolated
        return f"MERGE {json.dumps({f: q.value}, ensure_ascii=False)}"
    raise ValueError(q.op)


def _returning(q: WriteQuery) -> str:
    if q.returning == "count":
        return ""
    return f" RETURN {q.returning.upper()} $current.id AS id, $current.{q.field} AS v"


# UPSERT only by id (on another predicate it inserts a record with no id)


def _node_update(q: WriteQuery, sp: WriteSyntax, rewrites: WriteRewrites) -> tuple:
    params: dict = {}
    change = _change(q, params)
    where = _where(q, sp, rewrites)
    t = q.table
    if q.op == "upsert":
        params["wid"] = q.target_id
        return f"UPDATE {t} {change} UPSERT{_returning(q)} WHERE id = :wid {_T}", params
    if sp.target == "sub":
        src = f"(SELECT FROM {t}" + (f" WHERE {where}" if where else "") + ")"
        return f"UPDATE {src} {change}{_returning(q)} {_T}", params
    if sp.target == "nest":
        return f"UPDATE {t} {change}{_returning(q)} WHERE id IN (SELECT id FROM {t} WHERE {where}) {_T}", params
    return f"UPDATE {t} {change}{_returning(q)}" + (f" WHERE {where}" if where else "") + f" {_T}", params


def _victims(q: WriteQuery, where: "str | None") -> str:
    return f"(SELECT id FROM {q.table}" + (f" WHERE {where}" if where else "") + ")"


# DELETE VERTEX cascades its edges, DELETE FROM a vertex class is refused, a doc delete
# leaves its incoming links (the cascade rewrites them first); DELETE takes no TIMEOUT
assumes("delete_vertex.cascades_edges", SQL, True)
assumes("delete.document.keeps_incoming_links", SQL, True)
assumes("link.deleted_target.reads_null", SQL, True)
works_around("O38")


def _node_delete(q: WriteQuery, sp: WriteSyntax, rewrites: WriteRewrites, data_model: str, schema: Schema) -> Statements:
    where = _where(q, sp, rewrites)
    t = q.table
    victims_tables = {nt.name for nt in schema.node_types} if schema.is_base(t) else {t}
    st = Statements(door=sp.door)
    if data_model == "graph":
        if sp.target == "sub":
            st.main = (f"DELETE VERTEX FROM (SELECT FROM {t}" + (f" WHERE {where}" if where else "") + ")", {})
        elif sp.target == "nest":
            st.main = (f"DELETE VERTEX {t} WHERE id IN {_victims(q, where)}", {})
        else:
            st.main = (f"DELETE VERTEX {t}" + (f" WHERE {where}" if where else ""), {})
        return st
    if data_model == "rel":
        for et in schema.edge_types:
            if et.from_type in victims_tables or et.to_type in victims_tables:
                v = _victims(q, where)
                st.prelude.append((f"DELETE FROM {et.name} WHERE src_id IN {v} OR dst_id IN {v}", {}))
    else:
        # after the delete, every link to a deleted record dangles (its `target.id` is
        # null): each source drops them -- in a transaction too, since no list is
        # rewritten before its target goes (O38's COMMIT failure)
        for et in schema.edge_types:
            if et.to_type in victims_tables:
                st.postlude.append((f"UPDATE {et.from_type} SET {et.name} = {et.name}[target.id IS NOT NULL] WHERE {et.name} CONTAINS (target.id IS NULL) {_T}", {}))
    if sp.target == "nest":
        st.main = (f"DELETE FROM {t} WHERE id IN {_victims(q, where)}", {})
    else:
        st.main = (f"DELETE FROM {t}" + (f" WHERE {where}" if where else ""), {})
    return st


# `SET e[<filter>].f = ...` answers its count and changes nothing, and a position past
# the end of a list counts the record and changes nothing: one statement per position,
# behind the size
assumes("update.list_position.sets_element", SQL, True)
assumes("update.list_position.past_end_counts_record", SQL, True)
# an untyped embedded number is an INTEGER when it fits: the arithmetic is the declared
# LONG column's through `asLong()`
assumes("arithmetic.as_long.exact", SQL, True)


def _doc_positions(q: WriteQuery, schema: Schema) -> tuple:
    """The doc data model's edge-attribute write: `UPDATE src SET e[k].f = ... WHERE
    e.size() > k AND p(e[k])` for every position k of the database's longest list of
    the edge type (asked of the database before the write: the Instance's copy may
    differ from what every data model holds) -- the filter on the element, evaluated by
    the engine."""
    et = schema.edge_type(q.edge_type)
    e, f = et.name, q.field
    elem = f"{e}[{{k}}]"
    params: dict = {}
    op = q.op.removeprefix("edge_")
    if op == "set":
        change, params["wv"] = f"SET {elem}.{f} = :wv", q.value
    elif op == "arith":
        as_long = ".asLong()" if schema.field(et.name, f, edge=True).type is FieldType.INT else ""
        change, params["wd"] = f"SET {elem}.{f} = {elem}.{f}{as_long} {q.sym} :wd", q.value
    elif op == "remove":
        change = f"REMOVE {elem}.{f}"
    else:
        raise ValueError(q.op)
    cond = f"{e}.size() > {{k}}" + (f" AND ({render_pred(q.where, f'{elem}.', element=True)})" if q.where is not None else "")
    if q.extra_notnull:
        cond += f" AND {elem}.{f} IS NOT NULL"
    return (f"SELECT max({e}.size()) AS n FROM {et.from_type}", (f"UPDATE {et.from_type} {change} WHERE {cond} {_T}", params))


def _edge_write(q: WriteQuery, sp: WriteSyntax, rewrites: WriteRewrites, data_model: str, before: Instance, after: Instance) -> Statements:
    et = before.schema.edge_type(q.edge_type)
    st = Statements(door=sp.door)
    if data_model == "doc":
        if sp.form == "position":
            st.positions = _doc_positions(q, before.schema)
        else:
            w = render_pred(q.where)
            st.prelude = [(f"UPDATE {et.from_type} SET {et.name} = {et.name}[NOT ({w})] WHERE {et.name} CONTAINS ({w}) {_T}", {})]
        return st
    where = _where(q, sp, rewrites)
    cond = f" WHERE eid IN (SELECT eid FROM {et.name} WHERE {where})" if sp.target == "nest" else (f" WHERE {where}" if where else "")
    if q.op == "edge_delete":
        verb = f"DELETE EDGE {et.name}" if data_model == "graph" else f"DELETE FROM {et.name}"
        st.main = (verb + cond, {})
        return st
    params: dict = {}
    change = _change(q, params)
    verb = f"UPDATE EDGE {et.name}" if data_model == "graph" and sp.form == "update_edge" else f"UPDATE {et.name}"
    st.main = (f"{verb} {change}{cond} {_T}", params)
    return st


def render(q: WriteQuery, sp: WriteSyntax, rewrites: WriteRewrites, data_model: str, before: Instance, after: Instance) -> Statements:
    if q.op in EDGE_OPS:
        st = _edge_write(q, sp, rewrites, data_model, before, after)
    elif q.op == "delete":
        st = _node_delete(q, sp, rewrites, data_model, before.schema)
    else:
        st = Statements(main=_node_update(q, sp, rewrites), door=sp.door)
    # every statement of the data model carries the filter's bind variables
    binds = dict(q.where.bind_vars) if q.where is not None else {}
    st.prelude = [(t, {**binds, **params}) for t, params in st.prelude]
    st.postlude = [(t, {**binds, **params}) for t, params in st.postlude]
    if st.positions is not None:
        size, (text, params) = st.positions
        st.positions = (size, (text, {**binds, **params}))
    if st.main is not None:
        st.main = (st.main[0], {**binds, **st.main[1]})
    return st


def build_rewrites(q: WriteQuery, rng: random.Random, schema: Schema, instance: Instance) -> WriteRewrites:
    return WriteRewrites()


assumes("statement.write.answers_row_count", SQL, True)
assumes("statement.atomic", SQL, False)


def execute(session, st) -> list:
    """Run one data model's statements; the compared statement's answer."""
    from ganda.orientdb.engine.connection import unwrap_script
    from ganda.orientdb.concrete_syntax.setup.common import resolve_refs

    if st.positions is not None:
        st = st.at_positions(session.sql(st.positions[0])[0].get("n") or 0)
    if st.door == "tx":
        if not st.prelude and not st.main:
            return []
        rows = session.script(st.script(), resolve_refs(st.params, session.rids))
        return unwrap_script(rows) if st.main else []
    for text, params in st.prelude:
        session.sql(text, resolve_refs(params, session.rids), timeout_ms=0)
    answer = session.sql(st.main[0], resolve_refs(st.main[1], session.rids), timeout_ms=0) if st.main else []
    for text, params in st.postlude:
        session.sql(text, resolve_refs(params, session.rids), timeout_ms=0)
    return answer


def shared_binds(st: Statements) -> dict:
    """The bind variables a bundle records for the case."""
    return st.params


def rewrite_params(rewrites: WriteRewrites) -> dict:
    return {}
