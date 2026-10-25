"""The concrete syntax catalog of the write IR: HOW each data model issues a
`WriteQuery` in AQL. A write runs ONCE per database, so a case carries no variants:
each data model draws its own syntax, and the three answers and the three resulting
states are compared against each other and against the Instance with the write
applied. The choices:

    axis     choice       the text
    filter   plain        `FILTER p`
             rw / rev     `FILTER rewrite(p)` / the reversed operands
             ar           the arithmetic rewrite (`(n.f + @c) op @pc`), never served
    target   table        `FOR n IN t FILTER p`
             nest         `FOR n IN t FILTER n.id IN (FOR m IN t FILTER p RETURN m.id)`:
                          the records chosen before any is written
             sub          `FOR n IN (FOR m IN t FILTER p RETURN m)`
    door     auto         auto-committed statements
             tx           every statement inside one stream transaction, committed
    form     update       `UPDATE n WITH {f: ...} IN t OPTIONS {mergeObjects: false}`
                          (`keepNull: false` for a remove; never a merge into a map value)
             replace      `REPLACE n WITH MERGE(n, {f: ...}) IN t` (`UNSET(n, 'f')`)
             deref / eids the doc data model's node-delete cascade: every source's
                          array filtered by `target NOT IN <the doomed engine ids>`
                          (read through the predicate) or by `eid NOT IN <the ids the
                          reference chose>`
             bracket / eids / rewrite   the doc data model's edge delete as an inline
                          filter of the predicate, by the relationship ids, or each
                          source's whole array re-set (the only doc form for an
                          attribute write: a link object is not addressed in place)

The answer: a count (`LET r = (<write> RETURN 1) RETURN {count: LENGTH(r)}`, a
data-modification subquery), or the matched records' `id` and the written field
before / after the write (`RETURN {id: OLD.id, v: OLD.f}`). A node delete removes the
incident edges (graph) or junction rows (rel) first, each through the predicate's own
victims; the doc data model rewrites the arrays pointing at a victim and the victim's
own array dies with the document. An edge write on the doc data model answers no count
(its statement is per source), so the case compares the graph and rel answers and
checks every state (`partial`). A write on the base class (the inheritance axis) has
no syntax: no one statement writes several collections.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

from ganda.core.query.ir.predicates import Predicate, reverse
from ganda.core.query.ir.write import EDGE_OPS, WriteQuery, edge_hits, node_hits
from ganda.core.data.generator import BASE_CLASS
from ganda.core.data.model import Instance, Schema
from ganda.arangodb.rules import assumes
from ganda.arangodb.rules.catalog import AQL
from ganda.arangodb.oracle import rewrites as _rewrites
from ganda.arangodb.concrete_syntax.predicate import render_predicate as render_pred
from ganda.arangodb.storage.dialect import document_key

# a data-modification statement answers no count: the write runs as a subquery whose
# rows are counted (`_counted`)
assumes("statement.write.answers_row_count", AQL, False)
# UPDATE merges a map value key by key and keeps a null unless told otherwise: every
# update door carries `mergeObjects: false`, a remove `keepNull: false`
assumes("update.map_value.merged_by_default", AQL, True)
assumes("update.null.kept_by_default", AQL, True)
# removing a vertex removes no edge: every data model cascades explicitly (`_node_delete`)
assumes("delete_vertex.cascades_edges", AQL, False)

FILTERS = ("plain", "rw", "rev", "ar")
TARGETS = ("table", "nest", "sub")
DOORS = ("auto", "tx")
_NODE_UPDATES = ("set", "arith", "null", "remove", "merge")


@dataclass(frozen=True)
class WriteSyntax:
    filter: str = "plain"
    target: str = "table"
    door: str = "auto"
    form: str = ""

    @property
    def tag(self) -> str:
        parts = [x for x, default in ((self.form, ""), (self.filter, "plain"), (self.target, "table"), (self.door, "auto")) if x != default]
        return "+".join(parts) or "plain"


@dataclass
class WriteRewrites:
    rw: object = None
    rev: object = None
    ar: object = None

    def pred(self, q: WriteQuery, name: str):
        return q.where if name == "plain" else getattr(self, name)


@dataclass
class Statements:
    """What one data model sends: `prelude` statements (a cascade, the doc data model's
    array rewrites) and the `main` statement whose answer is compared (None: nothing is
    compared on this data model); `collections` the ones a transaction writes."""

    prelude: list = field(default_factory=list)  # [(text, params)]
    main: "tuple | None" = None
    door: str = "auto"
    collections: list = field(default_factory=list)

    @property
    def params(self) -> dict:
        out: dict = {}
        for _t, p in self.prelude + ([self.main] if self.main else []):
            out.update(p)
        return out

    def display(self) -> str:
        texts = [t for t, _p in self.prelude] + ([self.main[0]] if self.main else [])
        return ("/* tx */ " if self.door == "tx" else "") + "; ".join(texts)


def forms(q: WriteQuery, data_model: str) -> tuple:
    if q.op == "delete" and data_model == "doc":
        return ("deref", "eids")
    if q.op in EDGE_OPS and data_model == "doc":
        return ("bracket", "eids", "rewrite") if q.op == "edge_delete" else ("rewrite",)
    if q.op in _NODE_UPDATES or (q.op in EDGE_OPS and q.op != "edge_delete"):
        return ("update", "replace")
    return ("",)


def all_syntaxes(q: WriteQuery, data_model: str) -> list:
    if not q.edge and q.table == BASE_CLASS:
        return []
    return [WriteSyntax(f, t, d, m) for m in forms(q, data_model) for f in FILTERS for t in TARGETS for d in DOORS]


def applicable(q: WriteQuery, sp: WriteSyntax, rewrites: WriteRewrites, data_model: str) -> bool:
    doc_edge = q.op in EDGE_OPS and data_model == "doc"
    if doc_edge:
        if sp.target != "table":
            return False
        if sp.form == "bracket":
            return q.where is not None and sp.filter in ("plain", "rw") and (sp.filter == "plain" or rewrites.rw is not None)
        return sp.filter == "plain"
    if sp.filter != "plain":
        if q.where is None or rewrites.pred(q, sp.filter) is None:
            return False
        if sp.filter == "ar" and q.op in EDGE_OPS:
            return False
    if q.op == "upsert":
        return sp.filter == "plain" and sp.target == "table"
    if sp.target == "nest" and q.where is None and not q.extra_notnull:
        return False
    return True


# --- rendering -------------------------------------------------------------------------------


def _where(q: WriteQuery, sp: WriteSyntax, rewrites: WriteRewrites, var: str, schema: Schema = None) -> "str | None":
    pred = rewrites.pred(q, sp.filter)
    text = render_pred(pred, var, schema=schema) if pred is not None else None
    if q.extra_notnull:
        return f"({text}) AND {var}.{q.field} != null" if text else f"{var}.{q.field} != null"
    return text


def _loop(q: WriteQuery, sp: WriteSyntax, rewrites: WriteRewrites, schema: Schema = None) -> str:
    """`FOR n IN ...` over the records the write touches, through the target form."""
    t = q.target
    idf = "eid" if q.edge else "id"
    where_n = _where(q, sp, rewrites, "n", schema)
    where_m = _where(q, sp, rewrites, "m", schema)
    if sp.target == "sub":
        return f"FOR n IN (FOR m IN {t}" + (f" FILTER {where_m}" if where_m else "") + " RETURN m)"
    if sp.target == "nest":
        return f"FOR n IN {t} FILTER n.{idf} IN (FOR m IN {t}" + (f" FILTER {where_m}" if where_m else "") + f" RETURN m.{idf})"
    return f"FOR n IN {t}" + (f" FILTER {where_n}" if where_n else "")


def _change(q: WriteQuery, sp: WriteSyntax, params: dict) -> str:
    """The modification clause over `n` through the form (`update` / `replace`)."""
    op = q.op.removeprefix("edge_")
    f, t = q.field, q.target
    replace = sp.form == "replace"
    if op in ("set", "upsert"):
        params["wv"] = q.value
        return f"REPLACE n WITH MERGE(n, {{{f}: @wv}}) IN {t}" if replace else f"UPDATE n WITH {{{f}: @wv}} IN {t} OPTIONS {{mergeObjects: false}}"
    if op == "arith":
        params["wd"] = q.value
        return f"REPLACE n WITH MERGE(n, {{{f}: n.{f} {q.sym} @wd}}) IN {t}" if replace else f"UPDATE n WITH {{{f}: n.{f} {q.sym} @wd}} IN {t} OPTIONS {{mergeObjects: false}}"
    if op == "null":
        return f"REPLACE n WITH MERGE(n, {{{f}: null}}) IN {t}" if replace else f"UPDATE n WITH {{{f}: null}} IN {t} OPTIONS {{keepNull: true, mergeObjects: false}}"
    if op == "remove":
        return f"REPLACE n WITH UNSET(n, '{f}') IN {t}" if replace else f"UPDATE n WITH {{{f}: null}} IN {t} OPTIONS {{keepNull: false, mergeObjects: false}}"
    if op == "merge":
        params["wm"] = {f: q.value}
        return f"REPLACE n WITH MERGE(n, @wm) IN {t}" if replace else f"UPDATE n WITH @wm IN {t} OPTIONS {{mergeObjects: false}}"
    raise ValueError(q.op)


def _counted(loop: str, change: str) -> str:
    return f"LET r = ({loop} {change} RETURN 1) RETURN {{count: LENGTH(r)}}"


def _node_update(q: WriteQuery, sp: WriteSyntax, rewrites: WriteRewrites, schema: Schema = None) -> tuple:
    params: dict = {}
    t = q.table
    if q.op == "upsert":
        params.update({"wid": q.target_id, "wkey": document_key(q.target_id.split("/", 1)[1]), "wv": q.value})
        return f"LET r = (UPSERT {{id: @wid}} INSERT {{_key: @wkey, id: @wid, {q.field}: @wv}} UPDATE {{{q.field}: @wv}} IN {t} RETURN 1) RETURN {{count: LENGTH(r)}}", params
    change = _change(q, sp, params)
    loop = _loop(q, sp, rewrites, schema)
    if q.returning == "count":
        return _counted(loop, change), params
    doc = "OLD" if q.returning == "before" else "NEW"
    return f"{loop} {change} RETURN {{id: {doc}.id, v: {doc}.{q.field}}}", params


def _victims(q: WriteQuery, sp: WriteSyntax, rewrites: WriteRewrites, column: str, schema: Schema = None) -> str:
    where = _where(q, sp, rewrites, "m", schema)
    return f"(FOR m IN {q.table}" + (f" FILTER {where}" if where else "") + f" RETURN m.{column})"


def _node_delete(q: WriteQuery, sp: WriteSyntax, rewrites: WriteRewrites, data_model: str, schema: Schema, instance: Instance) -> Statements:
    t = q.table
    st = Statements(door=sp.door, collections=[t])
    incident = [et for et in schema.edge_types if et.from_type == t or et.to_type == t]
    if data_model == "graph":
        gone = _victims(q, sp, rewrites, "_id", schema)
        for et in incident:
            st.prelude.append((f"LET gone = {gone} FOR e IN {et.name} FILTER e._from IN gone OR e._to IN gone REMOVE e IN {et.name}", {}))
            st.collections.append(et.name)
    elif data_model == "rel":
        gone = _victims(q, sp, rewrites, "id", schema)
        for et in incident:
            st.prelude.append((f"LET gone = {gone} FOR e IN {et.name} FILTER e.src_id IN gone OR e.dst_id IN gone REMOVE e IN {et.name}", {}))
            st.collections.append(et.name)
    else:
        for et in [et for et in schema.edge_types if et.to_type == t]:
            arr = f"(s.{et.name} || [])"
            if sp.form == "eids":
                victims = {n.record_id for n in node_hits(q, instance)}
                eids = [e.record_id for e in instance.edges_of(et.name) if e.to_record in victims]
                if not eids:
                    continue
                bind = f"wg_{et.name}"
                st.prelude.append((f"FOR s IN {et.from_type} FILTER LENGTH(INTERSECTION({arr}[*].eid, @{bind})) > 0 UPDATE s WITH {{{et.name}: {arr}[* FILTER CURRENT.eid NOT IN @{bind}]}} IN {et.from_type}", {bind: eids}))
            else:
                gone = _victims(q, sp, rewrites, "_id", schema)
                st.prelude.append((f"LET gone = {gone} FOR s IN {et.from_type} FILTER LENGTH(INTERSECTION({arr}[*].target, gone)) > 0 UPDATE s WITH {{{et.name}: {arr}[* FILTER CURRENT.target NOT IN gone]}} IN {et.from_type}", {}))
            if et.from_type not in st.collections:
                st.collections.append(et.from_type)
    st.main = (_counted(_loop(q, sp, rewrites, schema), f"REMOVE n IN {t}"), {})
    return st


def _doc_rewrites(q: WriteQuery, before: Instance, after: Instance) -> list:
    """Each source holding a matched relationship re-sets its whole array to the one
    the Instance after the write holds."""
    from ganda.arangodb.storage.mapping.document import link_object

    et = before.schema.edge_type(q.edge_type)
    sources = sorted({e.from_record for e in edge_hits(q, before)})
    out = []
    for i, src in enumerate(sources):
        entries = [link_object(e) for e in after.edges_of(et.name) if e.from_record == src]
        out.append((f"UPDATE {{_key: @wk{i}}} WITH {{{et.name}: @wl{i}}} IN {et.from_type}", {f"wk{i}": document_key(src.split("/", 1)[1]), f"wl{i}": entries}))
    return out


def _edge_write(q: WriteQuery, sp: WriteSyntax, rewrites: WriteRewrites, data_model: str, before: Instance, after: Instance) -> Statements:
    et = before.schema.edge_type(q.edge_type)
    st = Statements(door=sp.door, collections=[et.name])
    if data_model == "doc":
        st.collections = [et.from_type]
        arr = f"(s.{et.name} || [])"
        if sp.form == "rewrite":
            st.prelude = _doc_rewrites(q, before, after)
        elif sp.form == "eids":
            gone = [e.record_id for e in edge_hits(q, before)]
            st.prelude = [(f"FOR s IN {et.from_type} FILTER LENGTH(INTERSECTION({arr}[*].eid, @wgone)) > 0 UPDATE s WITH {{{et.name}: {arr}[* FILTER CURRENT.eid NOT IN @wgone]}} IN {et.from_type}", {"wgone": gone})]
        else:
            w = render_pred(rewrites.pred(q, sp.filter), "CURRENT")
            st.prelude = [(f"FOR s IN {et.from_type} FILTER LENGTH({arr}[* FILTER {w}]) > 0 UPDATE s WITH {{{et.name}: {arr}[* FILTER NOT ({w})]}} IN {et.from_type}", {})]
        return st
    loop = _loop(q, sp, rewrites, before.schema)
    if q.op == "edge_delete":
        st.main = (_counted(loop, f"REMOVE n IN {et.name}"), {})
        return st
    params: dict = {}
    st.main = (_counted(loop, _change(q, sp, params)), params)
    return st


def render(q: WriteQuery, sp: WriteSyntax, rewrites: WriteRewrites, data_model: str, before: Instance, after: Instance) -> Statements:
    if q.op in EDGE_OPS:
        st = _edge_write(q, sp, rewrites, data_model, before, after)
    elif q.op == "delete":
        st = _node_delete(q, sp, rewrites, data_model, before.schema, before)
    else:
        st = Statements(main=_node_update(q, sp, rewrites, before.schema), door=sp.door, collections=[q.table])
    # every statement of the data model carries the filter's bind variables (its rewrite's)
    pred = rewrites.pred(q, sp.filter)
    binds = dict(pred.bind_vars) if pred is not None else {}
    st.prelude = [(t, {**binds, **params}) for t, params in st.prelude]
    if st.main is not None:
        st.main = (st.main[0], {**binds, **st.main[1]})
    return st


def build_rewrites(q: WriteQuery, rng: random.Random, schema: Schema, instance: Instance) -> WriteRewrites:
    rewrites = WriteRewrites()
    p = q.where
    if p is None:
        return rewrites
    rewrites.rw = _rewrites.rewrite(p, rng, "q")
    rewrites.rev = reverse(p)
    if isinstance(p, Predicate) and not q.edge:
        try:
            ftype = schema.field(q.table, p.field).type
        except KeyError:
            ftype = None
        if ftype is not None:
            stored = [n.values.get(p.field) for n in instance.nodes_of(q.table) if p.field in n.values]
            rewrites.ar = _rewrites.arithmetic(p, rng, "ar", ftype, stored)
    return rewrites


def execute(session, st: Statements) -> list:
    """Run one data model's statements; the compared statement's answer."""
    if not st.prelude and not st.main:
        return []
    if st.door == "tx":
        trx = session.begin_transaction(st.collections)
        try:
            for text, params in st.prelude:
                session.aql(text, params, trx=trx)
            rows = session.aql(st.main[0], st.main[1], trx=trx) if st.main else []
        except Exception:
            try:
                session.abort_transaction(trx)
            except Exception:  # noqa: BLE001 -- the first error is the one to report
                pass
            raise
        session.commit_transaction(trx)
        return rows
    for text, params in st.prelude:
        session.aql(text, params)
    return session.aql(st.main[0], st.main[1]) if st.main else []


def shared_binds(st: Statements) -> dict:
    """The bind variables a bundle records for the case: the doc data model's per-source
    arrays stay out of the shared map."""
    return {k: v for k, v in st.params.items() if not re.fullmatch(r"w[lk]\d+", k)}


def rewrite_params(rewrites: WriteRewrites) -> dict:
    params: dict = {}
    for name in ("rw", "rev", "ar"):
        rewritten = getattr(rewrites, name)
        if rewritten is not None:
            params[name] = rewritten.to_dict()
    return params
