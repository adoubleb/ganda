"""The setup steps of ArangoDB's data models and what the three mappings share.

ArangoDB has no multi-statement script language: schema lives behind REST endpoints
(`/_api/collection`, `/_api/index`) and data goes through the document API, AQL or a
stream transaction. A step is a plain dataclass with a JSON `to_dict()`, so a bug
bundle records the exact plan that ran (`ganda.core.engine.setup_plan`); its op name is
prefixed `arango_` (the step registry is shared by every provider):

    CreateCollection(name, edge)       `POST /_api/collection`
    CreateIndex(collection, fields, name, unique, sparse)   a persistent index
    Insert(collection, documents, via) a bulk insert: the document API (`api`), or
                                       `FOR d IN @docs INSERT d INTO c` (`aql`)
    Aql(query, bind_vars)              one AQL statement
    DocumentApi(method, collection, key, body, params)   PATCH / PUT / DELETE one document
    Transaction(steps, collections, commit)   the steps in one stream transaction

**No engine id appears in a plan as something to capture.** A document's `_key` is a
function of its logical id (`dialect.document_key`), so `_from`, `_to` and a link's
`target` are known before anything is written, and a plan is the same text on every
replay. The data steps are chunked (`insert_steps`): at most `WriteForm.chunk_size`
documents per request (the `statements_per_request` axis; the whole collection when 0)
and always under half the request bound (`ganda.arangodb.engine.limits`).

What the three data models share: the node documents (byte-identical values, the
logical `id` on every one), the churn discipline (initial values first, then the update
to the final ones through the data model's drawn form; churned relationships written
then removed; churned-away nodes removed with the per-model cascade -- the engine
removes no incident edge on its own), the write forms and the cancelled-junk block.
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field

from ganda.core.data.generator import initial_node_values, initial_nodes  # noqa: F401
from ganda.core.data.model import EdgeUpdate, FieldType, Instance, NodeUpdate, Schema, WriteForm
from ganda.core.engine.setup_plan import churned_away_edges, initial_edge_values, initial_edges, step, step_from_dict  # noqa: F401
from ganda.arangodb.rules import assumes
from ganda.arangodb.rules.catalog import AQL, DOCUMENT_API
from ganda.arangodb.storage.dialect import DEFAULT_ID_INDEX, UNIQUE_KINDS, document_key, engine_id
from ganda.arangodb.engine.limits import LIMITS

# every merging door (UPDATE / UPSERT / PATCH) carries `mergeObjects: false`, and unsets
# the fields the final state lacks in its own `keepNull: false` step
assumes("update.map_value.merged_by_default", AQL, True)
assumes("update.map_value.merged_by_default", DOCUMENT_API, True)
assumes("update.null.kept_by_default", AQL, True)
assumes("update.null.kept_by_default", DOCUMENT_API, True)
# the engine removes no incident edge on its own: each mapping cascades explicitly
assumes("delete_vertex.cascades_edges", AQL, False)


# --- setup plan vocabulary ---------------------------------------------------------------


@step("arango_collection")
@dataclass
class CreateCollection:
    name: str
    edge: bool = False

    def to_dict(self) -> dict:
        return {"op": "arango_collection", "name": self.name, "edge": self.edge}

    @classmethod
    def from_dict(cls, d: dict) -> "CreateCollection":
        return cls(d["name"], d.get("edge", False))


@step("arango_index")
@dataclass
class CreateIndex:
    collection: str
    fields: list
    name: str
    unique: bool = False
    sparse: bool = False

    def to_dict(self) -> dict:
        return {"op": "arango_index", "collection": self.collection, "fields": list(self.fields), "name": self.name, "unique": self.unique, "sparse": self.sparse}

    @classmethod
    def from_dict(cls, d: dict) -> "CreateIndex":
        return cls(d["collection"], list(d["fields"]), d["name"], d.get("unique", False), d.get("sparse", False))


@step("arango_insert")
@dataclass
class Insert:
    collection: str
    documents: list
    via: str = "api"

    def to_dict(self) -> dict:
        return {"op": "arango_insert", "collection": self.collection, "via": self.via, "documents": self.documents}

    @classmethod
    def from_dict(cls, d: dict) -> "Insert":
        return cls(d["collection"], d["documents"], d.get("via", "api"))


@step("arango_aql")
@dataclass
class Aql:
    query: str
    bind_vars: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"op": "arango_aql", "query": self.query, "bind_vars": self.bind_vars}

    @classmethod
    def from_dict(cls, d: dict) -> "Aql":
        return cls(d["query"], d.get("bind_vars") or {})


@step("arango_document")
@dataclass
class DocumentApi:
    method: str
    collection: str
    key: str
    body: "dict | None" = None
    params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"op": "arango_document", "method": self.method, "collection": self.collection, "key": self.key, "body": self.body, "params": dict(self.params)}

    @classmethod
    def from_dict(cls, d: dict) -> "DocumentApi":
        return cls(d["method"], d["collection"], d["key"], d.get("body"), d.get("params") or {})


@step("arango_transaction")
@dataclass
class Transaction:
    steps: list
    collections: list
    commit: bool = True

    def to_dict(self) -> dict:
        return {"op": "arango_transaction", "commit": self.commit, "collections": list(self.collections), "steps": [s.to_dict() for s in self.steps]}

    @classmethod
    def from_dict(cls, d: dict) -> "Transaction":
        return cls([step_from_dict(s) for s in d["steps"]], list(d.get("collections", [])), d.get("commit", True))


# --- DDL ----------------------------------------------------------------------------------


def node_collections(schema: Schema) -> list:
    return [nt.name for nt in schema.node_types]


def collection_steps(schema: Schema) -> list:
    """The node collections (the base class of the inheritance axis is no collection:
    its fields sit on every node document, a query on it a union of the collections)."""
    return [CreateCollection(nt.name) for nt in schema.node_types]


def _index_fields(schema: Schema, ix) -> "list | None":
    """The attribute paths an index spec names: a LIST field expanded (`f[*]`, the
    array index over its elements); None for a map index (`BY KEY` / `BY VALUE` has no
    equivalent: the spec stays in the schema, inert)."""
    if ix.option("by") is not None:
        return None
    out = []
    for f in ix.all_fields:
        try:
            ftype = schema.field(ix.table, f, edge=ix.target == "edge").type
        except KeyError:
            ftype = None
        out.append(f"{f}[*]" if ftype is FieldType.LIST else f)
    return out


def _index_collections(schema: Schema, ix) -> list:
    """A base-class index covers every subclass: one index per node collection."""
    if ix.target == "node" and schema.is_base(ix.table):
        return node_collections(schema)
    return [ix.table]


def index_steps(schema: Schema, data_model: str) -> list:
    """The index on `id` of every node collection (unique unless the id index axis drew
    a plain one), then this data model's indexes."""
    kind = schema.id_index_kind(data_model) or DEFAULT_ID_INDEX
    steps = [CreateIndex(nt.name, ["id"], f"ix_{nt.name}_id", unique=kind in UNIQUE_KINDS) for nt in schema.node_types]
    for ix in schema.indexes:
        if not ix.applies_to(data_model):
            continue
        fields = _index_fields(schema, ix)
        if fields is None:
            continue
        for coll in _index_collections(schema, ix):
            steps.append(CreateIndex(coll, fields, ix.name(), unique=ix.kind in UNIQUE_KINDS, sparse=ix.ignore_nulls))
    return steps


# --- data -----------------------------------------------------------------------------------


def node_document(node, values: dict, extra: "dict | None" = None) -> dict:
    doc = {"_key": document_key(node.key), "id": node.record_id}
    doc.update(values)
    if extra:
        doc.update(extra)
    return doc


def node_documents(instance: Instance, values_by_id: dict) -> dict:
    """collection -> [document] for every node ever written, with the given values."""
    out: dict = {nt.name: [] for nt in instance.schema.node_types}
    for n in initial_nodes(instance):
        out[n.type_name].append(node_document(n, values_by_id[n.record_id]))
    return out


# the body one request may carry: half the server's bound
_REQUEST_BUDGET = LIMITS.max_request_bytes // 2


def _doc_bytes(doc: dict) -> int:
    return len(json.dumps(doc, ensure_ascii=False, default=repr))


def chunks(docs: list, size: int) -> list:
    """`docs` in pieces of at most `size` documents (all of them when 0) whose JSON
    stays under the request budget; a document too big for the budget travels alone."""
    out: list = []
    chunk: list = []
    used = 0
    for d in docs:
        cost = _doc_bytes(d)
        if chunk and ((size and len(chunk) >= size) or used + cost > _REQUEST_BUDGET):
            out.append(chunk)
            chunk, used = [], 0
        chunk.append(d)
        used += cost
    if chunk:
        out.append(chunk)
    return out


# the model's PLAIN write form names every door `set`: here the document API, UPDATE
_PLAIN = {"set": "api"}
_PLAIN_UPDATE = {"set": "update"}


def insert_via(form: WriteForm) -> str:
    return _PLAIN.get(form.insert_via, form.insert_via)


def update_form(form: WriteForm) -> str:
    return _PLAIN_UPDATE.get(form.update_form, form.update_form)


def edge_update_form(form: WriteForm) -> str:
    return _PLAIN_UPDATE.get(form.edge_update_form, form.edge_update_form)


def insert_steps(docs_by_collection: dict, form: WriteForm) -> list:
    """The bulk inserts through the form's door, chunked."""
    steps = []
    for coll, docs in docs_by_collection.items():
        for chunk in chunks(docs, form.chunk_size):
            steps.append(Insert(coll, chunk, via=insert_via(form)))
    return steps


def node_update_steps(instance: Instance, form: WriteForm, extras: "dict | None" = None) -> list:
    """NodeUpdate churn: from the initial values the node was written with to its final
    `values`, through the data model's drawn update form. A whole-document form
    (REPLACE / PUT / `overwriteMode: replace`) carries `extras` (record id -> the
    attributes the data model keeps on the document besides its values: the doc data
    model's link arrays as they stand) so it does not wipe the relationship. A merging
    form (UPDATE / PATCH / UPSERT) unsets the fields the final state lacks in a separate
    `keepNull: false` step: folded into the final write it would strip an explicit
    final null too, and `HAS()` tells the two apart. Every merging form carries
    `mergeObjects: false`: AQL's default merges a map VALUE key by key into the stored
    one, so a map that lost a key would keep it (found live, seed 2 of the first smoke)."""
    steps = []
    extras = extras or {}
    for op in instance.churn:
        if not isinstance(op, NodeUpdate):
            continue
        n = op.node
        t, key = n.type_name, document_key(n.key)
        final = dict(n.values)
        full = {"id": n.record_id, **extras.get(n.record_id, {}), **final}
        dropped = {k: None for k in op.initial_values if k not in final}
        door = update_form(form)
        if door == "update":
            if dropped:
                steps.append(Aql(f"UPDATE @key WITH @doc IN {t} OPTIONS {{keepNull: false, mergeObjects: false}}", {"key": key, "doc": dropped}))
            steps.append(Aql(f"UPDATE @key WITH @doc IN {t} OPTIONS {{mergeObjects: false}}", {"key": key, "doc": final}))
        elif door == "replace":
            steps.append(Aql(f"REPLACE @key WITH @doc IN {t}", {"key": key, "doc": full}))
        elif door == "upsert":
            if dropped:
                steps.append(Aql(f"UPDATE @key WITH @doc IN {t} OPTIONS {{keepNull: false, mergeObjects: false}}", {"key": key, "doc": dropped}))
            steps.append(Aql(f"UPSERT {{_key: @key}} INSERT @ins UPDATE @doc IN {t} OPTIONS {{mergeObjects: false}}", {"key": key, "ins": {"_key": key, **full}, "doc": final}))
        elif door == "insert_overwrite":
            steps.append(Aql(f"INSERT @doc INTO {t} OPTIONS {{overwriteMode: 'replace'}}", {"doc": {"_key": key, **full}}))
        elif door == "patch_api":
            if dropped:
                steps.append(DocumentApi("PATCH", t, key, dropped, {"keepNull": "false", "mergeObjects": "false"}))
            steps.append(DocumentApi("PATCH", t, key, final, {"mergeObjects": "false"}))
        elif door == "put_api":
            steps.append(DocumentApi("PUT", t, key, full))
        else:
            raise ValueError(door)
    return steps


def edge_update_steps(instance: Instance, form: WriteForm, data_model: str) -> list:
    """EdgeUpdate churn on the edge collection (graph) or the junction (rel): the
    relationship's attributes go from their initial values to the final ones through
    the data model's edge update form (a REPLACE carries the endpoints and `eid`)."""
    steps = []
    for op in instance.churn:
        if not isinstance(op, EdgeUpdate):
            continue
        e = op.edge
        t, key = e.type_name, e.key
        final = dict(e.values)
        dropped = {k: None for k in op.initial_values if k not in final}
        if edge_update_form(form) == "replace":
            ends = {"_from": engine_id(e.from_record), "_to": engine_id(e.to_record)} if data_model == "graph" else {"src_id": e.from_record, "dst_id": e.to_record}
            steps.append(Aql(f"REPLACE @key WITH @doc IN {t}", {"key": key, "doc": {"eid": e.record_id, **ends, **final}}))
            continue
        if dropped:
            steps.append(Aql(f"UPDATE @key WITH @doc IN {t} OPTIONS {{keepNull: false, mergeObjects: false}}", {"key": key, "doc": dropped}))
        steps.append(Aql(f"UPDATE @key WITH @doc IN {t} OPTIONS {{mergeObjects: false}}", {"key": key, "doc": final}))
    return steps


JUNK_MARKER = "__junk"


def _free_value(ftype: FieldType, values: list):
    """A value of the field's own type no row holds -- what the junk document puts in
    a unique-indexed field. None when the type has no free value (a BOOL column holding
    both values)."""
    if ftype is FieldType.STRING:
        return JUNK_MARKER
    if ftype is FieldType.BINARY:
        return base64.b64encode(JUNK_MARKER.encode()).decode("ascii")
    present = [v for v in values if v is not None]
    if ftype in (FieldType.INT, FieldType.INT32):
        candidates = [424242, -424242, 2**30 + 1, -(2**30) - 1]
    elif ftype in (FieldType.FLOAT, FieldType.FLOAT32, FieldType.DECIMAL):
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


def cancelled_junk_steps(instance: Instance, data_model: str) -> list:
    """A stream transaction that inserts a junk document, rewrites and removes a real
    one, and is then ABORTED. Nothing in it may survive (the `cancel_leaks` mutant is
    the leak). The junk document fills every uniquely indexed field of the class with
    a free value so the insert is not rejected before the abort."""
    if not instance.nodes:
        return []
    schema = instance.schema
    victim = instance.nodes[0]
    t = victim.type_name
    junk: "dict | None" = {"_key": JUNK_MARKER, "id": f"{t}/{JUNK_MARKER}"}
    tables = {t} | ({schema.base.name} if schema.base is not None else set())
    for ix in schema.indexes:
        if ix.target == "node" and ix.table in tables and ix.kind in UNIQUE_KINDS and ix.applies_to(data_model):
            for f in ix.all_fields:
                spec = schema.field(t, f)
                v = _free_value(spec.type, [n.values.get(f) for n in instance.nodes_of(t)])
                if v is None:
                    junk = None
                    break
                junk[f] = v
            if junk is None:
                break
    key = document_key(victim.key)
    steps = []
    if junk is not None:
        steps.append(Aql(f"INSERT @doc INTO {t}", {"doc": junk}))
    steps.append(Aql(f"UPDATE @key WITH @doc IN {t} OPTIONS {{mergeObjects: false}}", {"key": key, "doc": {"junk": JUNK_MARKER}}))
    steps.append(Aql(f"REMOVE @key IN {t}", {"key": key}))
    return [Transaction(steps, [t], commit=False)]


# --- the bulk form of the churn (the `statements_per_request` axis) ---------------------------------

_FOLD = "FOR r IN @rows "
_BIND_RE = re.compile(r"@([A-Za-z_][A-Za-z0-9_]*)")


def _fold_text(text: str) -> str:
    """`UPDATE @key WITH @doc IN t ...` as `FOR r IN @rows UPDATE r.key WITH r.doc IN t ...`."""
    return _FOLD + _BIND_RE.sub(r"r.\1", text)


def _row_key(step) -> "str | None":
    """The document a churn statement addresses, so a folded statement never names one
    twice (a document modified twice in one AQL statement is a write conflict)."""
    if isinstance(step, DocumentApi):
        return step.key
    b = step.bind_vars
    if "key" in b:
        return b["key"]
    for name in ("doc", "ins"):
        if isinstance(b.get(name), dict) and "_key" in b[name]:
            return b[name]["_key"]
    return None


def _signature(step):
    if isinstance(step, Aql):
        return ("aql", step.query, tuple(sorted(step.bind_vars)))
    if isinstance(step, DocumentApi) and step.key:
        return ("api", step.method, step.collection, tuple(sorted(step.params.items())))
    return None


def _emit(sig, steps: list, size: int) -> list:
    """The steps of one signature as bulk statements of at most `size` documents each."""
    out: list = []
    chunk: list = []
    keys: set = set()
    for step in steps:
        k = _row_key(step)
        if chunk and (len(chunk) >= size or (k is not None and k in keys)):
            out.append(_bulk(sig, chunk))
            chunk, keys = [], set()
        chunk.append(step)
        if k is not None:
            keys.add(k)
    if chunk:
        out.append(_bulk(sig, chunk))
    return out


def _bulk(sig, chunk: list):
    if len(chunk) == 1:
        return chunk[0]
    if sig[0] == "aql":
        return Aql(_fold_text(chunk[0].query), {"rows": [dict(s.bind_vars) for s in chunk]})
    first = chunk[0]
    return DocumentApi(first.method, first.collection, "", [{"_key": s.key, **(s.body or {})} for s in chunk], dict(first.params))


def fold_steps(data: list, size: int) -> list:
    """The churn's statements folded into bulk ones: every run of statements of ONE
    shape (the same text, or the same document-API call) becomes `FOR r IN @rows
    <statement over r.key, r.doc, ...>` (or one bulk PATCH / PUT) of at most `size`
    documents, emitted where the shape first occurred. Statements of different shapes
    keep their relative order, and the statements of one shape address distinct
    documents, so what each document goes through is the sequence it went through
    before (a dropped-fields step before its final one, an update before a removal).
    The engine's own bulk door, where a plan of a hundred thousand single statements
    (a hundred thousand requests) is what a large instance would otherwise be."""
    groups: dict = {}
    slots: list = []
    for step in data:
        sig = _signature(step)
        if sig is None:
            slots.append(step)
            continue
        if sig not in groups:
            groups[sig] = []
            slots.append(sig)
        groups[sig].append(step)
    out: list = []
    for slot in slots:
        if isinstance(slot, tuple):
            out.extend(_emit(slot, groups[slot], size))
        else:
            out.append(slot)
    return out


def _rows_of(step) -> int:
    if isinstance(step, Insert):
        return len(step.documents)
    if isinstance(step, Aql) and step.query.startswith(_FOLD):
        return len(step.bind_vars.get("rows", ()))
    if isinstance(step, DocumentApi) and not step.key:
        return len(step.body or ())
    return 1


def transaction_groups(data: list, size: int) -> list:
    """The data steps in groups of about `size` documents each: one stream transaction
    per group. A stream transaction's cost grows with its write batch (one holding a
    whole large setup ran for an hour), so the transaction door at scale commits
    every `size` documents, as the OrientDB port's `BEGIN ... COMMIT` scripts do."""
    out: list = []
    group: list = []
    rows = 0
    for step in data:
        n = _rows_of(step)
        if group and rows + n > size:
            out.append(group)
            group, rows = [], 0
        group.append(step)
        rows += n
    if group:
        out.append(group)
    return out


def finalize(ddl: list, data: list, tail: list, instance: Instance, data_model: str, form: WriteForm, collections: list) -> list:
    """One data model's plan: the DDL, the data steps (folded into bulk statements and,
    under the transaction door, inside stream transactions -- one, or one per
    `chunk_size` documents under the `statements_per_request` axis), the tail, the
    cancelled junk block."""
    plan = list(ddl)
    if form.chunk_size:
        data = fold_steps(data, form.chunk_size)
    if form.transaction == "tx" and data:
        if form.chunk_size:
            plan += [Transaction(group, list(collections), commit=True) for group in transaction_groups(data, form.chunk_size)]
        else:
            plan.append(Transaction(data, list(collections), commit=True))
    else:
        plan.extend(data)
    plan.extend(tail)
    if form.cancelled_junk:
        plan += cancelled_junk_steps(instance, data_model)
    return plan
