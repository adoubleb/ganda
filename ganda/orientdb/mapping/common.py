"""Shared pieces of the three materializers, and the setup-plan vocabulary.

A materializer produces a `SetupPlan`: an ordered list of `Step` values that
`connection.OrientSession.apply_setup` executes -- `Sql` (one command with named
parameters), `Script` (a multi-statement SQL script, `BEGIN; ...; COMMIT;` or
`ROLLBACK;`) and `Batch` (the `/batch` endpoint, optionally transactional). Every step
is a plain dataclass with a JSON `to_dict()`, so a bug bundle records the exact plan
and `repro.py` replays it.

**RIDs never appear in a plan.** A record is addressed by its logical `id`; where a
statement needs a RID (a LINK inside an embedded document, `CREATE EDGE FROM :a TO :b`)
the parameter holds a `Ref("n1/r0")` placeholder, and the connection layer substitutes the RID
it captured when the step that inserted that record ran (`Sql.capture` names the id
to record). A plan is therefore the same text on every replay although every database
assigns its own RIDs.

What the three data models share: node records with byte-identical values (declared
properties of the same type on every data model), the logical `id` on every node and `eid`
on every relationship, the churn discipline (initial values first, then update to the
final ones through the data model's drawn update form; churned edges written then removed;
churned-away nodes removed with the per-side cascade), the write forms and the
cancelled-junk block.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ganda.orientdb.data_generator import initial_node_values, initial_nodes
from ganda.orientdb.model import (
    INT_TYPES,
    EdgeDelete,
    EdgeUpdate,
    FieldType,
    IndexSpec,
    Instance,
    NodeDelete,
    NodeUpdate,
    Schema,
    WriteForm,
)
from ganda.orientdb.dialect import ORIENT_TYPES, UNIQUE_KINDS


# --- RID placeholders -------------------------------------------------------------------

@dataclass(frozen=True)
class Ref:
    """A placeholder for the RID of the record whose logical id is `id`."""

    id: str


def encode_refs(value: Any) -> Any:
    """JSON form: a `Ref` becomes `{"$ref": id}`."""
    if isinstance(value, Ref):
        return {"$ref": value.id}
    if isinstance(value, list):
        return [encode_refs(v) for v in value]
    if isinstance(value, dict):
        return {k: encode_refs(v) for k, v in value.items()}
    return value


def decode_refs(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            return Ref(value["$ref"])
        return {k: decode_refs(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode_refs(v) for v in value]
    return value


def resolve_refs(value: Any, rids: dict) -> Any:
    """Substitute every `Ref` with the RID string the connection layer captured."""
    if isinstance(value, Ref):
        try:
            return rids[value.id]
        except KeyError:
            raise KeyError(f"no RID captured for {value.id!r}; the plan references a record before inserting it") from None
    if isinstance(value, list):
        return [resolve_refs(v, rids) for v in value]
    if isinstance(value, dict):
        return {k: resolve_refs(v, rids) for k, v in value.items()}
    return value


# --- setup plan vocabulary ---------------------------------------------------------------

@dataclass
class Sql:
    """One SQL command with named parameters. `capture` is the logical id whose RID
    the connection layer records from the command's result (an INSERT / CREATE VERTEX returns
    the record with its `@rid`)."""

    command: str
    params: dict = field(default_factory=dict)
    capture: "str | None" = None

    def to_dict(self) -> dict:
        return {"op": "sql", "command": self.command, "params": encode_refs(self.params), "capture": self.capture}


@dataclass
class Script:
    """A multi-statement script (`POST /command/<db>/script`). `params` are shared by
    every statement in it. `captures` maps a script variable name to the logical id
    whose RID that variable holds (the script ends with `RETURN` of those)."""

    text: str
    params: dict = field(default_factory=dict)
    captures: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"op": "script", "text": self.text, "params": encode_refs(self.params), "captures": dict(self.captures)}


@dataclass
class Batch:
    """The `/batch` endpoint: `steps` are `Sql` commands run in one request, in one
    transaction when `transaction` (probed: a failing command rolls the batch back)."""

    steps: list
    transaction: bool = True

    def to_dict(self) -> dict:
        return {"op": "batch", "transaction": self.transaction, "steps": [s.to_dict() for s in self.steps]}


SetupPlan = list


def plan_to_dict(plan: list) -> list:
    return [s.to_dict() for s in plan]


def step_from_dict(d: dict):
    op = d["op"]
    if op == "sql":
        return Sql(d["command"], decode_refs(d.get("params", {})), d.get("capture"))
    if op == "script":
        return Script(d["text"], decode_refs(d.get("params", {})), d.get("captures", {}))
    if op == "batch":
        return Batch([step_from_dict(s) for s in d["steps"]], d.get("transaction", True))
    raise ValueError(op)


def plan_from_dict(items: list) -> list:
    return [step_from_dict(d) for d in items]


# --- DDL ----------------------------------------------------------------------------------

def class_steps(name: str, fields: tuple, superclass: "str | None" = None, extra_properties: "list | None" = None, declare_id: bool = True, abstract: bool = False, clusters: int = 1) -> list:
    """A class with every field declared (schemaful). `extra_properties` are
    `(name, orient type)` pairs the data model adds (the doc data model's link lists, the
    junction's foreign keys). A subclass of the generated base class inherits `id`
    (`declare_id=False`). A ci field gets its `ALTER PROPERTY ... COLLATE ci` right
    after its declaration, before any data; `clusters > 1` spreads the records over
    that many clusters."""
    steps = [Sql(f"CREATE CLASS {name}" + (f" EXTENDS {superclass}" if superclass else "") + (f" CLUSTERS {clusters}" if clusters > 1 else "") + (" ABSTRACT" if abstract else ""))]
    if declare_id:
        steps.append(Sql(f"CREATE PROPERTY {name}.id STRING"))
    for f in fields:
        steps.append(Sql(f"CREATE PROPERTY {name}.{f.name} {ORIENT_TYPES[f.type]}"))
        if f.collate:
            steps.append(Sql(f"ALTER PROPERTY {name}.{f.name} COLLATE {f.collate}"))
    for pname, ptype in extra_properties or []:
        steps.append(Sql(f"CREATE PROPERTY {name}.{pname} {ptype}"))
    return steps


def id_index_step(table: str, schema: Schema, data_model: str) -> list:
    kind = schema.id_index_kind(data_model)
    if kind == "none":
        return []
    return [Sql(f"CREATE INDEX {table}_id ON {table} (id) {kind}")]


def index_step(ix: IndexSpec) -> Sql:
    """The metadata is always explicit: the engine's default is `ignoreNullValues:
    false` (probed: a UNIQUE index created without metadata treats an absent field as
    the key `null` and rejects the second record lacking it), so `ignore_nulls` is
    only an axis when both values are rendered out. A map index names its data model
    (`m BY KEY` / `m BY VALUE`)."""
    fields = ", ".join(ix.all_fields) + (f" BY {ix.by}" if ix.by else "")
    metadata = f" METADATA {{ignoreNullValues: {'true' if ix.ignore_nulls else 'false'}}}"
    return Sql(f"CREATE INDEX {ix.name()} ON {ix.table} ({fields}) {ix.kind}{metadata}")


def node_ddl(schema: Schema, data_model: str, superclass: "str | None" = None, extra: "callable | None" = None) -> list:
    """Every node class; with the inheritance axis, the abstract base class first
    (`CREATE CLASS N0 [EXTENDS V] ABSTRACT`, declaring `id` and the base fields) and
    every node class `EXTENDS N0`."""
    steps = []
    if schema.base is not None:
        steps += class_steps(schema.base.name, schema.base.fields, superclass, abstract=True)
        superclass = schema.base.name
    for nt in schema.node_types:
        steps += class_steps(nt.name, nt.fields, superclass, extra(nt) if extra else None, declare_id=schema.base is None, clusters=nt.clusters)
        steps += id_index_step(nt.name, schema, data_model)
    for ix in schema.indexes:
        if ix.target == "node" and ix.applies_to(data_model):
            steps.append(index_step(ix))
    return steps


def edge_index_steps(schema: Schema, data_model: str) -> list:
    return [index_step(ix) for ix in schema.indexes if ix.target == "edge" and ix.applies_to(data_model)]


# --- data -----------------------------------------------------------------------------------

def node_document(node, values: dict, extra: "dict | None" = None) -> dict:
    doc = {"id": node.record_id}
    doc.update(values)
    if extra:
        doc.update(extra)
    return doc


def _set_clause(doc: dict, params: dict, prefix: str = "") -> str:
    parts = []
    for i, (k, v) in enumerate(doc.items()):
        pname = f"{prefix}v{i}"
        params[pname] = v
        parts.append(f"{k} = :{pname}")
    return ", ".join(parts)


def insert_step(table: str, doc: dict, form: WriteForm, vertex: bool = False) -> Sql:
    """One record through the form's insert door. `capture` records the RID under the
    record's logical id."""
    params: dict = {}
    verb = f"CREATE VERTEX {table}" if vertex else f"INSERT INTO {table}"
    if form.insert_via == "content":
        params["doc"] = doc
        return Sql(f"{verb} CONTENT :doc", params, capture=doc["id"])
    if form.insert_via == "values" and not vertex:
        cols = ", ".join(doc)
        vals = ", ".join(f":v{i}" for i in range(len(doc)))
        for i, v in enumerate(doc.values()):
            params[f"v{i}"] = v
        return Sql(f"INSERT INTO {table} ({cols}) VALUES ({vals})", params, capture=doc["id"])
    return Sql(f"{verb} SET {_set_clause(doc, params)}", params, capture=doc["id"])


def _arith_set_clause(table: str, schema: Schema, initial: dict, final: dict, params: dict) -> str:
    """`f = f + :d` for every integer field present before and after (the delta keeps
    the sum a long: both values are of the column's type), `g = :v` for the rest."""
    parts = []
    for i, (k, v) in enumerate(final.items()):
        pname = f"v{i}"
        old = initial.get(k)
        try:
            ftype = schema.field(table, k).type
        except KeyError:
            ftype = None
        if ftype in INT_TYPES and isinstance(old, int) and not isinstance(old, bool) and isinstance(v, int) and not isinstance(v, bool) and -(2**63) <= v - old < 2**63:
            # the delta travels as a JSON integer, which must itself be a long
            # (campaign c100, seed 605: a delta of 2^63 + 24 was rejected at parsing)
            params[pname] = v - old
            parts.append(f"{k} = {k} + :{pname}")
        else:
            params[pname] = v
            parts.append(f"{k} = :{pname}")
    return ", ".join(parts)


def node_update_steps(instance: Instance, form: WriteForm, extras: "dict | None" = None) -> list:
    """NodeUpdate churn: from the initial values to the final `values`, through the
    data model's drawn update form. `extras` (record id -> fields the data model stores on the
    record besides its values, i.e. the doc data model's link lists as they stand) is merged
    into a CONTENT write so it does not wipe the relationship. A merging form (SET /
    MERGE / UPSERT / arith) also REMOVEs fields the final state lacks -- a separate
    step, or an explicit final null would be stripped too and `IS DEFINED` would
    differ."""
    steps = []
    extras = extras or {}
    for op in instance.churn:
        if not isinstance(op, NodeUpdate):
            continue
        n = op.node
        final = dict(n.values)
        dropped = [k for k in op.initial_values if k not in final]
        t = n.type_name
        if form.update_form == "content":
            # CONTENT takes a JSON literal, never a bind parameter (probed); a RID string
            # inside the literal would stay a string, so the doc data model's link lists are
            # re-set through a parameter right after the whole-document write
            doc = node_document(n, final)
            steps.append(Sql(f"UPDATE {t} CONTENT {json.dumps(doc, ensure_ascii=False)} WHERE id = :id", {"id": n.record_id}))
            for edge_type, entries in (extras.get(n.record_id) or {}).items():
                steps.append(Sql(f"UPDATE {t} SET {edge_type} = :links WHERE id = :id", {"links": entries, "id": n.record_id}))
            continue
        if dropped:
            steps.append(Sql(f"UPDATE {t} REMOVE {', '.join(dropped)} WHERE id = :id", {"id": n.record_id}))
        if not final:
            continue
        params: dict = {"id": n.record_id}
        if form.update_form == "merge":
            steps.append(Sql(f"UPDATE {t} MERGE {json.dumps(final, ensure_ascii=False)} WHERE id = :id", params))
        elif form.update_form == "upsert":
            steps.append(Sql(f"UPDATE {t} SET {_set_clause(final, params)} UPSERT WHERE id = :id", params))
        elif form.update_form == "arith":
            steps.append(Sql(f"UPDATE {t} SET {_arith_set_clause(t, instance.schema, op.initial_values, final, params)} WHERE id = :id", params))
        else:
            steps.append(Sql(f"UPDATE {t} SET {_set_clause(final, params)} WHERE id = :id", params))
    return steps


def initial_edge_values(instance: Instance) -> dict:
    """edge id -> the attribute values the relationship was FIRST written with."""
    out = {e.record_id: e.values for e in initial_edges(instance)}
    for op in instance.churn:
        if isinstance(op, EdgeUpdate):
            out[op.edge.record_id] = op.initial_values
    return out


def edge_update_steps(instance: Instance, form: WriteForm, verb: str) -> list:
    """EdgeUpdate churn on the graph E class (`verb` = "UPDATE EDGE") or the junction
    class ("UPDATE"): the relationship's attributes go from their initial values to
    the final ones through the data model's edge update form; dropped attributes are
    REMOVEd first (the CONTENT form rewrites the whole record: `eid` and, on the
    junction, the endpoint columns travel in the literal)."""
    steps = []
    for op in instance.churn:
        if not isinstance(op, EdgeUpdate):
            continue
        e = op.edge
        t = e.type_name
        final = dict(e.values)
        dropped = [k for k in op.initial_values if k not in final]
        params: dict = {"k": e.record_id}
        if form.edge_update_form == "content":
            doc = {"eid": e.record_id}
            if verb == "UPDATE":
                doc.update({"src_id": e.from_record, "dst_id": e.to_record})
            doc.update(final)
            steps.append(Sql(f"{verb} {t} CONTENT {json.dumps(doc, ensure_ascii=False)} WHERE eid = :k", params))
            continue
        if dropped:
            steps.append(Sql(f"{verb} {t} REMOVE {', '.join(dropped)} WHERE eid = :k", {"k": e.record_id}))
        if not final:
            continue
        if form.edge_update_form == "merge":
            steps.append(Sql(f"{verb} {t} MERGE {json.dumps(final, ensure_ascii=False)} WHERE eid = :k", params))
        else:
            steps.append(Sql(f"{verb} {t} SET {_set_clause(final, params)} WHERE eid = :k", params))
    return steps


def initial_edges(instance: Instance) -> list:
    """Every edge that was ever written, including ones churn later removes."""
    gone = churned_away_edges(instance)
    both = list(instance.edges) + gone
    return sorted(both, key=lambda e: (e.type_name, e.local_id))


def churned_away_edges(instance: Instance) -> list:
    out = [op.edge for op in instance.churn if isinstance(op, EdgeDelete)]
    for op in instance.churn:
        if isinstance(op, NodeDelete):
            out.extend(op.incident_edges)
    return out


JUNK_MARKER = "__junk"


def _free_value(ftype: FieldType, values: list):
    """A value of the field's own type no row holds -- what the junk record puts in
    a unique-indexed field. None when the type has no free value (a BOOL column holding
    both values under a unique index)."""
    if ftype is FieldType.STRING:
        return JUNK_MARKER
    present = [v for v in values if v is not None]
    if ftype is FieldType.INT:
        candidates = [2**61 + 1, -(2**61) - 1, 424242, -424242]
    elif ftype is FieldType.INT32:
        candidates = [424242, -424242, 2**30 + 1, -(2**30) - 1]
    elif ftype in (FieldType.FLOAT, FieldType.DECIMAL):
        candidates = [2.5e300, -2.5e300, 4242.5, -4242.5] if ftype is FieldType.FLOAT else [4242.5, -4242.5, 424242.125, -424242.125]
    elif ftype is FieldType.FLOAT32:
        candidates = [4242.5, -4242.5, 42.125, -42.125]
    elif ftype in (FieldType.DATETIME, FieldType.DATE):
        candidates = ["2222-02-22 00:00:00", "2333-03-03 00:00:00", "1911-11-11 00:00:00"]
    elif ftype is FieldType.BOOL:
        candidates = [True, False]
    elif ftype is FieldType.MAP:
        return {"kjunk": 424242}
    else:
        return [424242]
    for cand in candidates:
        if not any(type(cand) is type(v) and cand == v for v in present):
            return cand
    return None


def cancelled_junk_steps(instance: Instance, data_model: str, vertex: bool = False) -> list:
    """A `BEGIN; ...; ROLLBACK;` script that inserts a junk record, updates and deletes
    a real one. Nothing in it may survive (the `cancel_leaks` mutant is the leak). The
    junk record fills every uniquely indexed field with a free value so the insert is
    not rejected before the rollback."""
    if not instance.nodes:
        return []
    victim = instance.nodes[0]
    t = victim.type_name
    junk: "dict | None" = {"id": f"{t}/{JUNK_MARKER}"}
    for ix in instance.schema.indexes:
        if ix.target == "node" and ix.table == t and ix.kind in UNIQUE_KINDS and ix.applies_to(data_model):
            for f in ix.all_fields:
                spec = instance.schema.field(t, f)
                v = _free_value(spec.type, [n.values.get(f) for n in instance.nodes_of(t)])
                if v is None:
                    junk = None
                    break
                junk[f] = v
            if junk is None:
                break
    parts = ["BEGIN;"]
    params: dict = {"victim": victim.record_id}
    if junk is not None:
        params["junk"] = junk
        parts.append(f"{'CREATE VERTEX' if vertex else 'INSERT INTO'} {t} CONTENT :junk;")
    parts.append(f"UPDATE {t} SET id = :victim WHERE id = :victim;")
    parts.append(f"{'DELETE VERTEX' if vertex else 'DELETE FROM'} {t} WHERE id = :victim;")
    parts.append("ROLLBACK; RETURN 1;")
    return [Script(" ".join(parts), params)]


def _refs_in(value: Any) -> set:
    if isinstance(value, Ref):
        return {value.id}
    if isinstance(value, list):
        return set().union(*(_refs_in(v) for v in value)) if value else set()
    if isinstance(value, dict):
        return set().union(*(_refs_in(v) for v in value.values())) if value else set()
    return set()


def _has_complex_param(step: Sql) -> bool:
    return any(isinstance(v, (dict, list)) for v in step.params.values())


def _groups(data: list) -> list:
    """Split the data steps into runs that can be folded into one script or batch: a
    run closes before a step whose `Ref` names a record captured INSIDE the run (its RID
    does not exist until the run has executed). Every run is a list of `Sql`."""
    runs: list = []
    current: list = []
    captured_in_run: set = set()
    for step in data:
        if not isinstance(step, Sql):
            if current:
                runs.append(current)
                current, captured_in_run = [], set()
            runs.append([step])
            continue
        needs = _refs_in(step.params)
        if needs & captured_in_run:
            runs.append(current)
            current, captured_in_run = [], set()
        current.append(step)
        if step.capture:
            captured_in_run.add(step.capture)
    if current:
        runs.append(current)
    return runs


def wrap_data(data: list, form: WriteForm) -> list:
    """The data steps through the form's transaction door: plain commands, `/batch`
    requests with `transaction: true`, or `BEGIN ... COMMIT` scripts. A run of steps
    is folded only up to the first step whose `Ref` points at a record the same run
    inserts (`_groups`). The batch door takes positional parameters only and turns a
    map or list parameter into its `toString()` (probed: `{x=1}`), so a step carrying
    one runs as a plain command between two batches. A script or batch answers with
    the LAST command's result only, so captures inside one are re-read by the connection layer
    (`Script.captures` / the batch steps' `capture`)."""
    if form.transaction == "none" or not data:
        return data
    out: list = []
    for run in _groups(data):
        if len(run) == 1 and not isinstance(run[0], Sql):
            out.extend(run)
            continue
        if form.transaction == "batch":
            batchable: list = []
            for step in run:
                if _has_complex_param(step):
                    if batchable:
                        out.append(Batch(batchable, transaction=True))
                        batchable = []
                    out.append(step)
                else:
                    batchable.append(step)
            if batchable:
                out.append(Batch(batchable, transaction=True))
            continue
        params: dict = {}
        parts = ["BEGIN;"]
        captures: dict = {}
        for i, step in enumerate(run):
            text = step.command
            for k, v in step.params.items():
                nk = f"s{i}_{k}"
                params[nk] = v
                text = _rename_param(text, k, nk)
            if step.capture:
                parts.append(f"LET $c{i} = {text};")
                captures[f"$c{i}"] = step.capture
            else:
                parts.append(f"{text};")
        parts.append("COMMIT; RETURN 1;")
        out.append(Script(" ".join(parts), params, captures))
    return out


def _rename_param(text: str, old: str, new: str) -> str:
    import re

    return re.sub(rf":{re.escape(old)}(?![A-Za-z0-9_])", f":{new}", text)


def finalize(ddl: list, data: list, tail: list, instance: Instance, data_model: str, form: WriteForm, vertex: bool = False) -> list:
    plan = list(ddl) + wrap_data(data, form) + list(tail)
    if form.cancelled_junk:
        plan += cancelled_junk_steps(instance, data_model, vertex=vertex)
    if form.rebuild_indexes:
        plan.append(Sql("REBUILD INDEX *"))
    return plan
