"""The concrete syntax catalog of the write AST: HOW each SAP HANA data model issues a
`WriteQuery`. A write runs once per database, so each data model draws its own
syntax; the answers and the resulting states are compared with each other and with the
Instance after the write.

    axis     choice     the text
    filter   plain      the filter as drawn
             rw / rev   an equivalent tree / the reversed operands
             ar         the arithmetic rewrite (`(f + :c) op :pc`)
    target   table      `UPDATE t SET ... WHERE p`
             nest       `... WHERE id IN (SELECT id FROM t WHERE p)` (rel, doc: the
                        records chosen before any is written)
             merge      rel: `MERGE INTO t USING (SELECT id FROM t WHERE p) s ON ...`
    door     auto       auto-commit statements
             tx         every statement in one transaction, committed

What each data model answers -- the statement's row count, or the matched records
before / after the write:

    rel   UPDATE / DELETE / UPSERT row counts; a LIST field's child rows rewritten
          for the records the write matched (their ids: the reference's), after the
          compared statement; a node delete removes the junction rows first
    doc   UPDATE ... SET / UNSET and DELETE on the collection, their row counts; a node
          delete rewrites the link arrays of the documents pointing at a victim, and an
          edge write rewrites the link arrays of the sources it touches (the document
          store cannot address one element of an array by a filter), so an edge write
          compares no count on doc (`partial`) and its state is checked
    rdf   SPARQL Update has no row count: the count is a `SELECT (COUNT(*) ...)` with
          the same filter, run first; the matched records are marked, then written by
          plain DELETE / INSERT ... WHERE steps over the mark (`_rdf`); an edge delete
          rebuilds its type's direct links from the relationships left

`before` is a SELECT run before the write; `after` captures the matched ids first and
reads them back after it.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field

from ganda.core.ast.predicates import reverse
from ganda.core.ast.write import EDGE_OPS, WriteQuery, edge_hits, node_hits
from ganda.core.model import FieldType, Instance, Schema
from ganda.hana import rewrites as _rewrites
from ganda.hana.concrete_syntax import document
from ganda.hana.concrete_syntax.predicate import sparql_filter, sql
from ganda.hana.dialect import GRAPH_TOKEN, PREFIX_TEXT, child_table, list_text, record_iri, sparql_literal, sparql_string

FILTERS = ("plain", "rw", "rev", "ar")
TARGETS = ("table", "nest", "merge")
DOORS = ("auto", "tx")
_NODE_UPDATES = ("set", "arith", "null", "remove", "merge")
CAPTURED = "{{captured}}"


@dataclass(frozen=True)
class WriteSyntax:
    filter: str = "plain"
    target: str = "table"
    door: str = "auto"

    @property
    def tag(self) -> str:
        parts = [x for x, default in ((self.filter, "plain"), (self.target, "table"), (self.door, "auto")) if x != default]
        return "+".join(parts) or "plain"


@dataclass
class WriteRewrites:
    rw: object = None
    rev: object = None
    ar: object = None

    def pred(self, q: WriteQuery, name: str):
        return q.where if name == "plain" else getattr(self, name)


@dataclass
class Step:
    """One statement. `role`: run (no answer), count (its row count is the answer),
    count_rows (its single `v` is the count), rows (its rows are the answer), capture
    (its rows -- `id`, and the field's value as `v` -- are kept), rows_captured (its
    rows, read with the captured ids as `:captured`), each_run / each_count (run once
    per captured row with its id as `:cid`, and with `arith` = (sym, constant, type)
    the row's value shifted as `:cnew`; the row counts summed are the answer)."""

    text: str
    params: dict = field(default_factory=dict)
    role: str = "run"
    arith: "tuple | None" = None


@dataclass
class Statements:
    steps: list = field(default_factory=list)
    door: str = "auto"

    @property
    def main(self):
        """The compared statement, or None (nothing compared on this data model)."""
        return next((s for s in self.steps if s.role in ("count", "count_rows", "rows", "rows_captured", "each_count")), None)

    @property
    def params(self) -> dict:
        out: dict = {}
        for s in self.steps:
            out.update(s.params)
        return out

    def display(self) -> str:
        body = " ;\n".join(f"[{s.role}] {s.text}" for s in self.steps)
        return f"[tx] {body}" if self.door == "tx" else body


def all_syntaxes(q: WriteQuery, data_model: str) -> list:
    return [WriteSyntax(f, t, d) for f in FILTERS for t in TARGETS for d in DOORS]


def applicable(q: WriteQuery, sp: WriteSyntax, rewrites: WriteRewrites, data_model: str) -> bool:
    if sp.filter != "plain" and (q.where is None or rewrites.pred(q, sp.filter) is None):
        return False
    pred = rewrites.pred(q, sp.filter)
    if q.op == "upsert":
        return sp.filter == "plain" and sp.target == "table"
    if data_model == "rdf" and sp.target != "table":
        return False
    if sp.target == "merge" and (data_model != "rel" or q.op not in ("set", "merge", "null", "remove", "arith")):
        return False
    if sp.target == "nest" and q.where is None and not (data_model == "doc" and q.op == "arith"):
        return False
    if data_model == "doc":
        if q.edge:
            return sp.filter == "plain" and sp.target == "table"
        if not (document.doc_filter_ok(pred) or not document.compares_list_text(pred)):
            return False
        if sp.target == "table":
            return q.op != "arith" and update_filter_ok(pred)
    return True


# --- shared ----------------------------------------------------------------------------------


def _ftype(q: WriteQuery, schema: Schema) -> FieldType:
    if q.edge:
        return schema.field(q.edge_type, q.field, edge=True).type
    return schema.field(q.table, q.field).type


def _new_value(q: WriteQuery):
    op = q.op.removeprefix("edge_")
    return None if op in ("null", "remove") else q.value


def _ids(rows) -> list:
    return [r.record_id for r in rows]


# --- rel --------------------------------------------------------------------------------------


def _rel_where(q: WriteQuery, pred, key: str, table: str) -> "str | None":
    text = sql(pred, "", table, key) if pred is not None else None
    if q.extra_notnull:
        return f"({text}) AND {q.field} IS NOT NULL" if text else f"{q.field} IS NOT NULL"
    return text


def _children(table: str, f: str, ids: list, value) -> list:
    """A LIST field's child rows of `ids`, rewritten to `value`'s elements."""
    steps = [Step(f"DELETE FROM {child_table(table, f)} WHERE owner IN :wids", {"wids": ids})] if ids else []
    for k, rid in enumerate(ids):
        for pos, v in enumerate(value or []):
            steps.append(Step(f"INSERT INTO {child_table(table, f)} (owner, pos, v) VALUES (:wo{k}, {pos}, :wv__{pos})", {f"wo{k}": rid, "wv": value}))
    return steps


def _rel(q: WriteQuery, sp: WriteSyntax, pred, schema: Schema, before: Instance) -> Statements:
    st = Statements(door=sp.door)
    if q.edge:
        table, key = q.edge_type, "eid"
        hits = edge_hits(q, before)
    else:
        table, key = q.table, "id"
        hits = node_hits(q, before)
    where = _rel_where(q, pred, key, table)
    if sp.target == "nest" and where:
        where = f"{key} IN (SELECT {key} FROM {table} WHERE {where})"
    w = f" WHERE {where}" if where else ""
    if q.op in ("delete", "edge_delete"):
        if q.op == "delete":
            for et in schema.edge_types:
                if et.from_type == table or et.to_type == table:
                    victims = f"(SELECT id FROM {table}{w})"
                    gone = f"SELECT eid FROM {et.name} WHERE src_id IN {victims} OR dst_id IN {victims}"
                    for f in et.fields:
                        if f.type is FieldType.LIST:
                            st.steps.append(Step(f"DELETE FROM {child_table(et.name, f.name)} WHERE owner IN ({gone})"))
                    st.steps.append(Step(f"DELETE FROM {et.name} WHERE src_id IN {victims} OR dst_id IN {victims}"))
        st.steps.append(Step(f"DELETE FROM {table}{w}", role="count"))
        owner_fields = schema.edge_type(table).fields if q.edge else schema.query_fields(table)
        ids = _ids(hits)
        for f in owner_fields:
            if f.type is FieldType.LIST and ids:
                st.steps.append(Step(f"DELETE FROM {child_table(table, f.name)} WHERE owner IN :wgone", {"wgone": ids}))
        return st
    ftype = _ftype(q, schema)
    f = q.field
    if q.op == "upsert":
        value = list_text(q.value) if ftype is FieldType.LIST else q.value
        st.steps.append(Step(f"UPSERT {table} (id, {f}) VALUES (:wid, :wv) WHERE id = :wid", {"wid": q.target_id, "wv": value}, "count"))
        if ftype is FieldType.LIST:
            st.steps += _children(table, f, [q.target_id], q.value)
        return st
    new = _new_value(q)
    params: dict = {}
    if q.op.endswith("arith"):
        params["wd"] = q.value
        change = f"{f} = {f} {q.sym} :wd"
    elif new is None:
        change = f"{f} = NULL"
    else:
        params["wv"] = list_text(new) if ftype is FieldType.LIST else new
        change = f"{f} = :wv"
    if not q.edge and q.returning == "before":
        st.steps.append(Step(f"SELECT id, {f} AS {'v__json' if ftype is FieldType.LIST else 'v'} FROM {table}{w}", role="rows"))
    if not q.edge and q.returning == "after":
        st.steps.append(Step(f"SELECT id FROM {table}{w}", role="capture"))
    role = "count" if q.returning == "count" or q.edge else "run"
    if sp.target == "merge":
        st.steps.append(Step(f"MERGE INTO {table} USING (SELECT {key} FROM {table}{w}) s ON {table}.{key} = s.{key} WHEN MATCHED THEN UPDATE SET {table}.{change}", params, role))
    else:
        st.steps.append(Step(f"UPDATE {table} SET {change}{w}", params, role))
    if not q.edge and q.returning == "after":
        st.steps.append(Step(f"SELECT id, {f} AS {'v__json' if ftype is FieldType.LIST else 'v'} FROM {table} WHERE id IN :captured", role="rows_captured"))
    if ftype is FieldType.LIST:
        st.steps += _children(table, f, _ids(hits), new)
    return st


# --- doc --------------------------------------------------------------------------------------

_KEY = 'WHERE $."id" = CAST(:{} AS NVARCHAR(256))'
_UPDATE_OPS = ("=", "<>", "<", "<=", ">", ">=", "BETWEEN", "IN", "IS NULL", "IS NOT NULL")


def update_filter_ok(p) -> bool:
    """Whether a filter is in the UPDATE / DELETE statement's documented filter
    language: comparisons of a path with a value, BETWEEN, IS NULL, AND, OR (a negation
    pushed down onto them), over scalar fields."""
    if p is None:
        return True
    return document.doc_filter_ok(p) and all(leaf.op in _UPDATE_OPS and leaf.ftype != "list" for leaf in p.leaves())


def _doc_value(bind: str) -> str:
    """A written value: its JSON text parsed, so the store keeps what an INSERT of the
    same document keeps (a number with a fraction a DECIMAL, an integer a BIGINT)."""
    return f"PARSE_JSON(:{bind}__json)"


def _link_arrays(q: WriteQuery, schema: Schema, after: Instance, sources: dict, edge_types) -> list:
    """Each source document holding a touched relationship re-sets its whole link array
    of each edge type to the Instance after the write."""
    steps = []
    k = 0
    for et in edge_types:
        for src in sorted(sources.get(et.name, ())):
            links = [{"eid": e.record_id, "to": e.to_record, **{a: v for a, v in e.values.items()}} for e in after.edges if e.type_name == et.name and e.from_record == src]
            steps.append(Step(f"UPDATE {et.from_type} SET {document.path(et.name)} = PARSE_JSON(:wl{k}) {_KEY.format(f'wi{k}')}", {f"wl{k}": json.dumps(links, ensure_ascii=False), f"wi{k}": src}))
            k += 1
    return steps


def _doc(q: WriteQuery, sp: WriteSyntax, pred, schema: Schema, before: Instance, after: Instance) -> Statements:
    """`table`: one UPDATE / DELETE with the filter (where the statement's filter
    language states it); `nest`: the records chosen first by a collection SELECT (the
    full filter language), then written one by one by id -- the only form for an
    arithmetic write, which the UPDATE statement cannot compute: the value read is
    shifted and written back."""
    st = Statements(door=sp.door)
    if q.edge:
        et = schema.edge_type(q.edge_type)
        sources = {et.name: {e.from_record for e in edge_hits(q, before)}}
        st.steps = _link_arrays(q, schema, after, sources, [et])
        return st
    t = q.table
    f = q.field
    if q.op == "upsert":
        if before.node_by_id(q.target_id) is not None:
            st.steps.append(Step(f"UPDATE {t} SET {document.path(f)} = {_doc_value('wv')} {_KEY.format('wid')}", {"wid": q.target_id, "wv": q.value}, "count"))
        else:
            st.steps.append(Step(f"INSERT INTO {t} VALUES(:wdoc)", {"wdoc": json.dumps({"id": q.target_id, f: q.value}, ensure_ascii=False)}, "count"))
        return st
    stated = document.doc_filter_ok(pred)
    where = document.doc_pred(pred) if pred is not None and stated else None
    if q.extra_notnull:
        where = f"({where}) AND {document.path(f)} IS NOT NULL" if where else f"{document.path(f)} IS NOT NULL"
    w = f" WHERE {where}" if where else ""
    by_id = sp.target == "nest" or q.op == "arith" or not stated
    ftype = _ftype(q, schema) if f else None
    alias = {FieldType.INT: "v__int", FieldType.FLOAT: "v__float", FieldType.BOOL: "v__bool", FieldType.LIST: "v__json"}.get(ftype, "v")

    def chosen(cols: str) -> str:
        """The matched records: a collection query where its filter language states
        the filter, else the views read in SQL (`document`)."""
        if stated:
            return f"SELECT {document.path('id')} AS id{cols} FROM {t}{w}"
        conds = [sql(pred, "t", t, "id", document.NAMES)] + ([f"t.{f} IS NOT NULL"] if q.extra_notnull else [])
        view_cols = cols.replace(document.path(f), f"t.{f}") if f else cols
        return document.with_views(schema, f"SELECT t.id AS id{view_cols} FROM {document.NAMES.table(t)} t WHERE {' AND '.join(conds)}")

    if by_id or q.returning == "after":
        st.steps.append(Step(chosen(f", {document.path(f)} AS v__int" if q.op == "arith" else ""), role="capture"))
    if q.op == "delete":
        gone = {n.record_id for n in node_hits(q, before)}
        incoming = [et for et in schema.edge_types if et.to_type == t]
        sources = {et.name: {e.from_record for e in before.edges if e.type_name == et.name and e.to_record in gone and e.from_record not in gone} for et in incoming}
        st.steps += _link_arrays(q, schema, after, sources, incoming)
        st.steps.append(Step(f"DELETE FROM {t} {_KEY.format('cid')}", role="each_count") if by_id else Step(f"DELETE FROM {t}{w}", role="count"))
        return st
    new = _new_value(q)
    params: dict = {}
    path = document.path(f)
    arith = None
    if q.op == "arith":
        change, arith = f"SET {path} = {_doc_value('cnew')}", (q.sym, q.value, ftype.value)
    elif q.op == "null":
        change = f"SET {path} = NULL"
    elif new is None:
        change = f"UNSET {path}"
    else:
        params["wv"] = new
        change = f"SET {path} = {_doc_value('wv')}"
    if q.returning == "before":
        st.steps.append(Step(chosen(f", {path} AS {alias}"), role="rows"))
    counted = q.returning == "count"
    if by_id:
        st.steps.append(Step(f"UPDATE {t} {change} {_KEY.format('cid')}", params, "each_count" if counted else "each_run", arith=arith))
    else:
        st.steps.append(Step(f"UPDATE {t} {change}{w}", params, "count" if counted else "run"))
    if q.returning == "after":
        st.steps.append(Step(f"SELECT {document.path('id')} AS id, {path} AS {alias} FROM {t} WHERE {document.path('id')} IN :captured", role="rows_captured"))
    return st


# --- rdf --------------------------------------------------------------------------------------


MARK = "<urn:ganda:w>"


def _rdf(q: WriteQuery, sp: WriteSyntax, pred, schema: Schema) -> Statements:
    """The matched records are MARKED first (`?n <urn:ganda:w> true`), then written by
    plain DELETE / INSERT ... WHERE steps over the mark, and the marks removed: no step
    has an OPTIONAL, a negation or a template variable a solution may leave unbound,
    and the filter is evaluated once, before anything is written (as the SQL forms
    evaluate theirs)."""
    st = Statements(door=sp.door)
    g = GRAPH_TOKEN

    def update(text: str, role: str = "run") -> Step:
        return Step(f"{PREFIX_TEXT} {text}", role=role)

    def marked(body: str) -> str:
        return f"WHERE {{ GRAPH {g} {{ ?n {MARK} true . {body} }} }}"

    if q.edge:
        cls, idp = f"c:{q.edge_type}", "p:eid"
    else:
        cls, idp = f"c:{q.table}", "p:id"
    f = q.field
    if q.op == "upsert":
        st.steps.append(Step(f"{PREFIX_TEXT} SELECT (1 AS ?v) WHERE {{ BIND (1 AS ?one) }}", role="count_rows"))
        n = record_iri(q.target_id)
        st.steps.append(update(f"DELETE WHERE {{ GRAPH {g} {{ {n} p:{f} ?o }} }}"))
        if isinstance(q.value, list) or _ftype(q, schema) is FieldType.LIST:
            st.steps.append(update(f"DELETE WHERE {{ GRAPH {g} {{ {n} i:{f} ?i }} }}"))
        values = [f"{n} a {cls}", f"{n} p:id {sparql_string(q.target_id)}"]
        if q.value is not None:
            values.append(f"{n} p:{f} {sparql_literal(q.value)}")
            if isinstance(q.value, list):
                values += [f"{n} i:{f} {sparql_literal(x)}" for x in sorted(set(q.value))]
        st.steps.append(update(f"INSERT DATA {{ GRAPH {g} {{ {' . '.join(values)} }} }}"))
        return st
    match = f"?n a {cls} ; {idp} ?id ."
    if pred is not None:
        match += " " + sparql_filter(pred, "n")
    if q.extra_notnull:
        match += f" ?n p:{f} ?ganda_present ."
    ftype = _ftype(q, schema) if f else None
    v = "?v__json" if ftype is FieldType.LIST else "?v"
    if not q.edge and q.returning == "before":
        st.steps.append(update(f"SELECT ?id {v} WHERE {{ GRAPH {g} {{ {match} OPTIONAL {{ ?n p:{f} {v} }} }} }}", "rows"))
    if not q.edge and q.returning == "after":
        st.steps.append(update(f"SELECT ?id WHERE {{ GRAPH {g} {{ {match} }} }}", "capture"))
    if q.returning == "count" or q.edge or q.op == "delete":
        st.steps.append(update(f"SELECT (COUNT(*) AS ?v) WHERE {{ GRAPH {g} {{ {match} }} }}", "count_rows"))
    st.steps.append(update(f"DELETE WHERE {{ GRAPH {g} {{ ?x {MARK} ?y }} }}"))
    st.steps.append(update(f"INSERT {{ GRAPH {g} {{ ?n {MARK} true }} }} WHERE {{ GRAPH {g} {{ {match} }} }}"))
    if q.op == "delete":
        st.steps.append(update(f"DELETE {{ GRAPH {g} {{ ?e ?ep ?eo }} }} {marked('?e p:src ?n ; ?ep ?eo .')}"))
        st.steps.append(update(f"DELETE {{ GRAPH {g} {{ ?e ?ep ?eo }} }} {marked('?e p:dst ?n ; ?ep ?eo .')}"))
        link_pred = '?s ?lp ?n . FILTER (STRSTARTS(STR(?lp), "urn:ganda:l:"))'
        st.steps.append(update(f"DELETE {{ GRAPH {g} {{ ?s ?lp ?n }} }} {marked(link_pred)}"))
        st.steps.append(update(f"DELETE {{ GRAPH {g} {{ ?n ?p ?o }} }} {marked('?n ?p ?o .')}"))
        return st
    if q.op == "edge_delete":
        e = q.edge_type
        st.steps.append(update(f"DELETE {{ GRAPH {g} {{ ?n ?p ?o }} }} {marked('?n ?p ?o .')}"))
        # the type's direct links rebuilt from the relationships left
        st.steps.append(update(f"DELETE WHERE {{ GRAPH {g} {{ ?s l:{e} ?t }} }}"))
        st.steps.append(update(f"INSERT {{ GRAPH {g} {{ ?s l:{e} ?t }} }} WHERE {{ GRAPH {g} {{ ?x a c:{e} ; p:src ?s ; p:dst ?t }} }}"))
        return st
    new = _new_value(q)
    if q.op.endswith("arith"):
        st.steps.append(update(f"DELETE {{ GRAPH {g} {{ ?n p:{f} ?o }} }} INSERT {{ GRAPH {g} {{ ?n p:{f} ?new }} }} {marked(f'?n p:{f} ?o . BIND (STRDT(STR(?o {q.sym} {sparql_literal(q.value)}), xsd:long) AS ?new)')}"))
    else:
        st.steps.append(update(f"DELETE {{ GRAPH {g} {{ ?n p:{f} ?o }} }} {marked(f'?n p:{f} ?o .')}"))
        if ftype is FieldType.LIST:
            st.steps.append(update(f"DELETE {{ GRAPH {g} {{ ?n i:{f} ?o }} }} {marked(f'?n i:{f} ?o .')}"))
        if new is not None:
            values = [f"?n p:{f} {sparql_literal(new)}"] + ([f"?n i:{f} {sparql_literal(x)}" for x in sorted(set(new))] if isinstance(new, list) else [])
            st.steps.append(update(f"INSERT {{ GRAPH {g} {{ {' . '.join(values)} }} }} {marked('')}"))
    st.steps.append(update(f"DELETE WHERE {{ GRAPH {g} {{ ?x {MARK} ?y }} }}"))
    if not q.edge and q.returning == "after":
        st.steps.append(update(f"SELECT ?id {v} WHERE {{ GRAPH {g} {{ VALUES ?id {{ {CAPTURED} }} ?n {idp} ?id . OPTIONAL {{ ?n p:{f} {v} }} }} }}", "rows_captured"))
    return st


# --- the contract -----------------------------------------------------------------------------


def render(q: WriteQuery, sp: WriteSyntax, rewrites: WriteRewrites, data_model: str, before: Instance, after: Instance) -> Statements:
    pred = rewrites.pred(q, sp.filter)
    schema = before.schema
    if data_model == "rel":
        st = _rel(q, sp, pred, schema, before)
    elif data_model == "doc":
        st = _doc(q, sp, pred, schema, before, after)
    else:
        st = _rdf(q, sp, pred, schema)
    binds = dict(pred.bind_vars) if pred is not None else {}
    for s in st.steps:
        s.params = {**binds, **s.params}
    return st


def build_rewrites(q: WriteQuery, rng: random.Random, schema: Schema, instance: Instance) -> WriteRewrites:
    rewrites = WriteRewrites()
    p = q.where
    if p is None:
        return rewrites
    rewrites.rw = _rewrites.rewrite(p, rng, "q")
    rewrites.rev = reverse(p)
    if not q.edge and p is not None and not hasattr(p, "children") and p.sub is None:
        try:
            ftype = schema.field(q.table, p.field).type
        except KeyError:
            ftype = None
        if ftype is not None:
            stored = [n.values.get(p.field) for n in instance.nodes_of(q.table) if p.field in n.values]
            rewrites.ar = _rewrites.arithmetic(p, rng, "ar", ftype, stored)
    return rewrites


def execute(session, st: Statements) -> list:
    """Run one data model's statements (in one transaction for the tx door); the
    compared statement's answer."""
    return session.run_statements(st.steps, transaction=st.door == "tx")


def shared_binds(st: Statements) -> dict:
    """The bind variables a bundle records for the case (the doc data model's
    per-source link arrays stay out)."""
    import re

    return {k: v for k, v in st.params.items() if not re.fullmatch(r"w[lio]\d+", k)}


def rewrite_params(rewrites: WriteRewrites) -> dict:
    params: dict = {}
    for name in ("rw", "rev", "ar"):
        rewritten = getattr(rewrites, name)
        if rewritten is not None:
            params[name] = rewritten.to_dict()
    return params
