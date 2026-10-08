"""The document data model: the JSON Document Store. One COLLECTION per node class; a
document holds the node's id and fields and, per edge type leaving its class, the array
of its relationships as link objects:

    n1: {"id": "n1/r0", "f1": 5, "f3": [1, 2], "e1": [{"eid": "e1/e0", "to": "n2/r3", "w": 3}]}

so a relationship lives INSIDE its source document, as a document model keeps it (an
outgoing hop is an UNNEST of the array, an incoming one a search of every source's
array). An explicit null is a JSON null, an absent field an absent key: both read as
null. A number is stored as the store decides (a BIGINT for an integer, a DECIMAL for a
number with a fraction or exponent, documented).

The setup writes the initial documents (a churned node with its first values, its
array with every relationship it ever had and their first attributes) through the
form's insert door -- a JSON string parameter, the object expression (`VALUES({"id":
'n1/r0', ...})`), or a batch of the prepared INSERT -- then replays the churn: a
NodeUpdate as `UPDATE ... SET ... UNSET ...` of the fields, as the whole document
replaced (`SET n1.$ = ...`), or as a delete and a re-insert; a relationship's churn
re-sets its source's link array; a NodeDelete re-sets the arrays pointing at the node
and deletes the document. The id index axis puts a HASH index on `id` (the store has no
unique index); a secondary index is a HASH index on an integer or string field (the
store indexes those for equality only).
"""
from __future__ import annotations

import json

from ganda.core.model import EdgeDelete, EdgeUpdate, FieldType, Instance, NodeDelete, NodeUpdate
from ganda.hana.concrete_syntax.document import path
from ganda.hana.mapping.common import Declare, Sql, SqlBatch, Transaction, initial_edge_values, initial_edges, initial_node_values, initial_nodes, wrap
from ganda.hana.mapping.relational import declared_types

DATA_MODEL = "doc"
_KEY = 'WHERE $."id" = CAST(:k AS NVARCHAR(256))'


def link_object(edge, values: dict) -> dict:
    return {"eid": edge.record_id, "to": edge.to_record, **values}


def _literal(v) -> str:
    """A value in the INSERT's object expression: a string in single quotes, a number
    as written, a boolean / null as the keyword, an array of its elements, an object."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    if isinstance(v, list):
        return "[" + ", ".join(_literal(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f'"{k}": {_literal(x)}' for k, x in v.items()) + "}"
    raise TypeError(v)


def _insert(table: str, doc: dict, via: str):
    if via == "select":
        return Sql(f"INSERT INTO {table} VALUES({_literal(doc)})")
    return Sql(f"INSERT INTO {table} VALUES(:doc)", {"doc": json.dumps(doc, ensure_ascii=False)})


def _inserts(docs: list, via: str) -> list:
    if via != "batch":
        return [_insert(t, d, via) for t, d in docs]
    out: list = []
    for t, d in docs:
        text = f"INSERT INTO {t} VALUES(:doc)"
        row = {"doc": json.dumps(d, ensure_ascii=False)}
        if out and out[-1].text == text:
            out[-1].rows.append(row)
        else:
            out.append(SqlBatch(text, [row]))
    return out


def ddl(instance: Instance) -> list:
    schema = instance.schema
    steps = [Declare(declared_types(instance))]
    id_index = schema.id_index_kind(DATA_MODEL) != "none"
    for nt in schema.node_types:
        steps.append(Sql(f"CREATE COLLECTION {nt.name}"))
        if id_index:
            steps.append(Sql(f"CREATE HASH INDEX ix_{nt.name}_id ON {nt.name}({path('id')})"))
    for ix in schema.indexes:
        if not ix.applies_to(DATA_MODEL) or ix.target != "node" or ix.extra_fields:
            continue
        if schema.field(ix.table, ix.field).type not in (FieldType.INT, FieldType.STRING):
            continue
        steps.append(Sql(f"CREATE HASH INDEX {ix.name()} ON {ix.table}({path(ix.field)})"))
    return steps


class _Documents:
    """The documents as the setup has written them so far (node values and link arrays)."""

    def __init__(self, instance: Instance):
        self.schema = instance.schema
        self.values: dict = {}
        self.type_of: dict = {}
        self.links: dict = {}  # (source id, edge type) -> [link object]

    def doc(self, rid: str) -> dict:
        out = {"id": rid, **self.values[rid]}
        for et in self.schema.edge_types_from(self.type_of[rid]):
            if (rid, et.name) in self.links:
                out[et.name] = self.links[(rid, et.name)]
        return out


def _field_update(table: str, fields, final: dict) -> Sql:
    params: dict = {"k": None}
    sets, unsets = [], []
    for i, f in enumerate(fields):
        v = final.get(f.name)
        if v is None:
            unsets.append(path(f.name))
        else:
            params[f"v{i}"] = v
            sets.append(f"{path(f.name)} = PARSE_JSON(:v{i}__json)")
    text = f"UPDATE {table}"
    if sets:
        text += " SET " + ", ".join(sets)
    if unsets:
        text += " UNSET " + ", ".join(unsets)
    return Sql(f"{text} {_KEY}", params)


def _set_array(docs: _Documents, source: str, et) -> Sql:
    links = docs.links.get((source, et.name), [])
    return Sql(f"UPDATE {et.from_type} SET {path(et.name)} = PARSE_JSON(:links) {_KEY}", {"links": json.dumps(links, ensure_ascii=False), "k": source})


def build_document_setup(instance: Instance) -> list:
    schema = instance.schema
    form = instance.write_form(DATA_MODEL)
    docs = _Documents(instance)
    first = initial_node_values(instance)
    nodes = sorted(initial_nodes(instance), key=lambda n: (n.type_name, n.local_id))
    for n in nodes:
        docs.values[n.record_id] = dict(first[n.record_id])
        docs.type_of[n.record_id] = n.type_name
    edge_first = initial_edge_values(instance)
    for e in initial_edges(instance):
        docs.links.setdefault((e.from_record, e.type_name), []).append(link_object(e, edge_first[e.record_id]))
    data = _inserts([(n.type_name, docs.doc(n.record_id)) for n in nodes], form.insert_via)
    for op in instance.churn:
        if isinstance(op, NodeUpdate):
            n = op.node
            docs.values[n.record_id] = dict(n.values)
            fields = schema.query_fields(n.type_name)
            if form.update_form == "upsert":
                data.append(Sql(f"UPDATE {n.type_name} SET {n.type_name}.$ = PARSE_JSON(:doc) {_KEY}", {"doc": json.dumps(docs.doc(n.record_id), ensure_ascii=False), "k": n.record_id}))
            elif form.update_form == "merge":
                data.append(Sql(f"DELETE FROM {n.type_name} {_KEY}", {"k": n.record_id}))
                data += _inserts([(n.type_name, docs.doc(n.record_id))], form.insert_via)
            else:
                step = _field_update(n.type_name, fields, n.values)
                step.params["k"] = n.record_id
                if len(step.params) > 1 or "UNSET" in step.text:
                    data.append(step)
        elif isinstance(op, (EdgeDelete, EdgeUpdate)):
            e = op.edge
            key = (e.from_record, e.type_name)
            if isinstance(op, EdgeDelete):
                docs.links[key] = [x for x in docs.links.get(key, []) if x["eid"] != e.record_id]
            else:
                docs.links[key] = [link_object(e, e.values) if x["eid"] == e.record_id else x for x in docs.links.get(key, [])]
            data.append(_set_array(docs, e.from_record, schema.edge_type(e.type_name)))
        elif isinstance(op, NodeDelete):
            gone = op.node.record_id
            touched = set()
            for e in op.incident_edges:
                key = (e.from_record, e.type_name)
                docs.links[key] = [x for x in docs.links.get(key, []) if x["eid"] != e.record_id]
                if e.from_record != gone:
                    touched.add(key)
            for source, et_name in sorted(touched):
                data.append(_set_array(docs, source, schema.edge_type(et_name)))
            data.append(Sql(f"DELETE FROM {op.node.type_name} {_KEY}", {"k": gone}))
    tail = []
    if form.cancelled_junk:
        junk = [(nt.name, {"id": f"{nt.name}/junk{i}"}) for nt in schema.node_types for i in range(2)]
        tail.append(Transaction(_inserts(junk, form.insert_via), commit=False))
    return ddl(instance) + wrap(data, form) + tail
