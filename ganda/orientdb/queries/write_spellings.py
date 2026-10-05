"""The spelling catalog of the WRITE IR: HOW each side issues a `WriteQuery`. A write
runs ONCE per database, so a case does not carry variant sides: each side draws its
own spelling, and the three answers and the three resulting states are compared
against each other and against the Instance with the write applied. Over a campaign
every spelling meets every other one. The choices:

    axis     choice       the text
    filter   plain        `WHERE p`
             rw / rev     `WHERE rewrite(p)` / the reversed operands (`:p < f`, a scan)
             ar           the select IR's arithmetic twin (`(f + :c) op :pc`), a scan
    target   table        `UPDATE t ... WHERE p`
             nest         `UPDATE t ... WHERE id IN (SELECT id FROM t WHERE p)`: the
                          records chosen before any is written
             sub          `UPDATE (SELECT FROM t WHERE p) ...`, a lazily streamed
                          subquery target (`DELETE VERTEX FROM (SELECT ...)` on the
                          graph side)
    door     auto         auto-commit commands
             tx           `BEGIN; ...; LET $r = <write>; COMMIT; RETURN $r`
    form     update_edge  the graph side's `UPDATE EDGE e ...` (else `UPDATE e ...`)
             bracket      the doc side's edge delete as `e = e[NOT (p)]`
             eids         ... as `e = e[eid NOT IN :gone]` (the ids the reference chose)
             deref        the doc side's node-delete cascade as `e = e[target.id NOT IN
                          :gone]` (through the link; in a transaction that also deletes
                          the target: O38)
             eids / rewrite  ... the edge delete by the ids the reference chose, or
                          each source's whole list re-set by id (the only doc
                          form for an edge-attribute write: an element of an embedded
                          list cannot be set in place, probed)

Per model: a node delete is `DELETE FROM t` (doc, rel) or `DELETE VERTEX t` (graph:
`DELETE FROM` a vertex or edge class is refused, probed), and its cascade is the
engine's own on the graph side, `DELETE FROM e WHERE src_id IN (SELECT id FROM t
WHERE p) OR dst_id IN (...)` first on the rel side, and `e = e[target.id NOT IN
:gone]` on every source pointing at a victim first on the doc side (after the delete
a LINK dangles and `target.id` is null). An edge write is `UPDATE EDGE` / `DELETE
EDGE` on the E class and a plain `UPDATE` / `DELETE FROM` of the junction; on the doc
side its answer is not the relationship count, so an edge case compares the graph and
rel answers and checks all three states (`partial`).

Every UPDATE carries `TIMEOUT <ms> EXCEPTION` (a runaway update is O37; `DELETE`
takes no TIMEOUT clause, probed).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from ganda.orientdb.model import Instance, Schema
from ganda.orientdb.queries.osql import render_predicate as render_pred
from ganda.orientdb.queries.write_ir import EDGE_OPS, WriteQuery, edge_hits, node_hits

FILTERS = ("plain", "rw", "rev", "ar")
TARGETS = ("table", "nest", "sub")
DOORS = ("auto", "tx")
WRITE_TIMEOUT_MS = 10000
_T = f"TIMEOUT {WRITE_TIMEOUT_MS} EXCEPTION"
# the ops whose statement is an UPDATE of one node field (a `sub` target applies)
_NODE_UPDATES = ("set", "arith", "null", "remove", "merge")


@dataclass(frozen=True)
class WriteSpelling:
    filter: str = "plain"
    target: str = "table"
    door: str = "auto"
    form: str = ""

    @property
    def tag(self) -> str:
        parts = [x for x, default in ((self.form, ""), (self.filter, "plain"), (self.target, "table"), (self.door, "auto")) if x != default]
        return "+".join(parts) or "plain"


@dataclass
class WriteTwins:
    rw: object = None
    rev: object = None
    ar: object = None

    def pred(self, q: WriteQuery, name: str):
        return q.where if name == "plain" else getattr(self, name)


@dataclass
class Statements:
    """What one side sends: `prelude` commands (a cascade, the doc side's list
    rewrites) and the `main` statement whose answer is compared (None: nothing is
    compared on this side)."""

    prelude: list = field(default_factory=list)  # [(text, params)]
    main: "tuple | None" = None
    door: str = "auto"

    @property
    def params(self) -> dict:
        out: dict = {}
        for _t, p in self.prelude + ([self.main] if self.main else []):
            out.update(p)
        return out

    def script(self) -> str:
        body = [t for t, _p in self.prelude]
        if self.main:
            body.append(f"LET $r = {self.main[0]}")
        text = "BEGIN; " + "; ".join(body) + "; COMMIT;"
        return text + (" RETURN $r" if self.main else "")

    def display(self) -> str:
        if self.door == "tx":
            return self.script()
        return "; ".join([t for t, _p in self.prelude] + ([self.main[0]] if self.main else []))


def forms(q: WriteQuery, side: str) -> tuple:
    if q.op == "delete" and side == "doc":
        # the cascade through the link (`target.id`) or by the relationship ids
        return ("deref", "eids")
    if q.op in EDGE_OPS and side == "doc":
        return ("bracket", "eids", "rewrite") if q.op == "edge_delete" else ("rewrite",)
    if q.op in EDGE_OPS and side == "graph" and q.op != "edge_delete":
        return ("update_edge", "update")
    return ("",)


def all_spellings(q: WriteQuery, side: str) -> list:
    return [WriteSpelling(f, t, d, m) for m in forms(q, side) for f in FILTERS for t in TARGETS for d in DOORS]


def applicable(q: WriteQuery, sp: WriteSpelling, twins: WriteTwins, side: str) -> bool:
    doc_edge = q.op in EDGE_OPS and side == "doc"
    if doc_edge:
        if sp.target != "table":
            return False
        if sp.form == "bracket":
            # the predicate itself in a bracket filter: plain or rewritten leaves only
            return q.where is not None and sp.filter in ("plain", "rw") and (sp.filter == "plain" or twins.rw is not None)
        return sp.filter == "plain"
    if sp.filter != "plain":
        if q.where is None or twins.pred(q, sp.filter) is None:
            return False
        if sp.filter == "ar" and q.op in EDGE_OPS:
            return False
    if q.op == "upsert":
        return sp.filter == "plain" and sp.target == "table"
    if sp.target == "nest" and q.where is None and not q.extra_notnull:
        return False
    if sp.target == "sub":
        return q.op in _NODE_UPDATES or (q.op == "delete" and side == "graph")
    return True


# --- rendering -------------------------------------------------------------------------------


def _where(q: WriteQuery, sp: WriteSpelling, twins: WriteTwins) -> "str | None":
    pred = twins.pred(q, sp.filter)
    text = render_pred(pred) if pred is not None else None
    if q.extra_notnull:
        return f"({text}) AND {q.field} IS NOT NULL" if text else f"{q.field} IS NOT NULL"
    return text


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


def _node_update(q: WriteQuery, sp: WriteSpelling, twins: WriteTwins) -> tuple:
    params: dict = {}
    change = _change(q, params)
    where = _where(q, sp, twins)
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


def _node_delete(q: WriteQuery, sp: WriteSpelling, twins: WriteTwins, side: str, schema: Schema, instance: Instance) -> Statements:
    where = _where(q, sp, twins)
    t = q.table
    victims_tables = {nt.name for nt in schema.node_types} if schema.is_base(t) else {t}
    st = Statements(door=sp.door)
    if side == "graph":
        if sp.target == "sub":
            st.main = (f"DELETE VERTEX FROM (SELECT FROM {t}" + (f" WHERE {where}" if where else "") + ")", {})
        elif sp.target == "nest":
            st.main = (f"DELETE VERTEX {t} WHERE id IN {_victims(q, where)}", {})
        else:
            st.main = (f"DELETE VERTEX {t}" + (f" WHERE {where}" if where else ""), {})
        return st
    if side == "rel":
        for et in schema.edge_types:
            if et.from_type in victims_tables or et.to_type in victims_tables:
                v = _victims(q, where)
                st.prelude.append((f"DELETE FROM {et.name} WHERE src_id IN {v} OR dst_id IN {v}", {}))
    elif sp.form == "eids":
        gone = {n.record_id for n in node_hits(q, instance)}
        for et in schema.edge_types:
            eids = [e.record_id for e in instance.edges_of(et.name) if e.to_record in gone]
            if eids:
                st.prelude.append((f"UPDATE {et.from_type} SET {et.name} = {et.name}[eid NOT IN :wg_{et.name}] WHERE {et.name} CONTAINS (eid IN :wg_{et.name}) {_T}", {f"wg_{et.name}": eids}))
    else:
        gone = [n.record_id for n in node_hits(q, instance)]
        for et in schema.edge_types:
            if et.to_type in victims_tables:
                st.prelude.append((f"UPDATE {et.from_type} SET {et.name} = {et.name}[target.id NOT IN :wgone] WHERE {et.name} CONTAINS (target.id IN :wgone) {_T}", {"wgone": gone}))
    if sp.target == "nest":
        st.main = (f"DELETE FROM {t} WHERE id IN {_victims(q, where)}", {})
    else:
        st.main = (f"DELETE FROM {t}" + (f" WHERE {where}" if where else ""), {})
    return st


def _doc_rewrites(q: WriteQuery, before: Instance, after: Instance) -> list:
    """Each source holding a matched relationship re-sets its whole list, by id, to
    the list the Instance after the write holds (targets as `Ref`s the harness
    resolves to this database's RIDs)."""
    from ganda.orientdb.materialize.document import link_entry

    et = before.schema.edge_type(q.edge_type)
    sources = sorted({e.from_record for e in edge_hits(q, before)})
    out = []
    for i, src in enumerate(sources):
        entries = [link_entry(e) for e in after.edges if e.type_name == q.edge_type and e.from_record == src]
        out.append((f"UPDATE {et.from_type} SET {et.name} = :wl{i} WHERE id = :wi{i} {_T}", {f"wl{i}": entries, f"wi{i}": src}))
    return out


def _edge_write(q: WriteQuery, sp: WriteSpelling, twins: WriteTwins, side: str, before: Instance, after: Instance) -> Statements:
    et = before.schema.edge_type(q.edge_type)
    st = Statements(door=sp.door)
    if side == "doc":
        if sp.form == "rewrite":
            st.prelude = _doc_rewrites(q, before, after)
        elif sp.form == "eids":
            gone = [e.record_id for e in edge_hits(q, before)]
            st.prelude = [(f"UPDATE {et.from_type} SET {et.name} = {et.name}[eid NOT IN :wgone] WHERE {et.name} CONTAINS (eid IN :wgone) {_T}", {"wgone": gone})]
        else:
            w = render_pred(twins.pred(q, sp.filter))
            st.prelude = [(f"UPDATE {et.from_type} SET {et.name} = {et.name}[NOT ({w})] WHERE {et.name} CONTAINS ({w}) {_T}", {})]
        return st
    where = _where(q, sp, twins)
    cond = f" WHERE eid IN (SELECT eid FROM {et.name} WHERE {where})" if sp.target == "nest" else (f" WHERE {where}" if where else "")
    if q.op == "edge_delete":
        verb = f"DELETE EDGE {et.name}" if side == "graph" else f"DELETE FROM {et.name}"
        st.main = (verb + cond, {})
        return st
    params: dict = {}
    change = _change(q, params)
    verb = f"UPDATE EDGE {et.name}" if side == "graph" and sp.form == "update_edge" else f"UPDATE {et.name}"
    st.main = (f"{verb} {change}{cond} {_T}", params)
    return st


def render(q: WriteQuery, sp: WriteSpelling, twins: WriteTwins, side: str, before: Instance, after: Instance) -> Statements:
    if q.op in EDGE_OPS:
        st = _edge_write(q, sp, twins, side, before, after)
    elif q.op == "delete":
        st = _node_delete(q, sp, twins, side, before.schema, before)
    else:
        st = Statements(main=_node_update(q, sp, twins), door=sp.door)
    # every statement of the side carries the filter's bind variables (its twin's)
    pred = twins.pred(q, sp.filter)
    binds = dict(pred.bind_vars) if pred is not None else {}
    st.prelude = [(t, {**binds, **params}) for t, params in st.prelude]
    if st.main is not None:
        st.main = (st.main[0], {**binds, **st.main[1]})
    return st
