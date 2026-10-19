"""The transaction door of the select IR (`SelectQuery.tx`): one record of the class
written (updated, inserted or deleted) inside a transaction, the question asked inside
it, the transaction rolled back -- so the answer is the Instance's with the write
applied, and the stored state afterwards must still be the Instance.

A door text is a SCRIPT: `TX_MARK`, then the statements separated by `SEPARATOR`, the
question last. The session runs them with auto-commit off, answers the last one and
rolls back (`connection.HanaSession.execute`). The knowledge graph's updates join the
SQL transaction (documented), so the rdf data model's door is the same script with a
SPARQL update in it.
"""
from __future__ import annotations

import json

from ganda.core.query.ir.select import TxWrite
from ganda.core.data.model import FieldType, Schema
from ganda.hana.concrete_syntax import document
from ganda.hana.dialect import GRAPH_TOKEN, PREFIX_TEXT, child_table, record_iri, sparql_literal, sparql_string

TX_MARK = "/* ganda:tx */"
SEPARATOR = "\n;;\n"


def script(statements: list, question: str) -> str:
    return TX_MARK + " " + SEPARATOR.join(statements + [question])


def is_script(text: str) -> bool:
    return text.startswith(TX_MARK)


def parts(text: str) -> list:
    return text[len(TX_MARK):].strip().split(SEPARATOR)


def _field(schema: Schema, tx: TxWrite):
    return schema.field(tx.table, tx.field)


def extra_binds(tx: TxWrite, schema: Schema) -> dict:
    """The doc data model's inserted document, as JSON text."""
    if tx.write != "insert":
        return {}
    doc = {"id": tx.tid, tx.field: tx.tv}
    return {"txdoc": json.dumps(doc, ensure_ascii=False)}


def _rel(tx: TxWrite, schema: Schema) -> list:
    t, f = tx.table, tx.field
    ftype = _field(schema, tx).type
    lists = [x for x in schema.query_fields(t) if x.type is FieldType.LIST]
    if tx.write == "delete":
        return [f"DELETE FROM {child_table(t, x.name)} WHERE owner = :tid" for x in lists] + [f"DELETE FROM {t} WHERE id = :tid"]
    value = None if tx.remove else tx.tv
    elements = []
    if ftype is FieldType.LIST:
        if tx.write == "update":
            elements.append(f"DELETE FROM {child_table(t, f)} WHERE owner = :tid")
        for k in range(len(value or [])):
            elements.append(f"INSERT INTO {child_table(t, f)} (owner, pos, v) VALUES (:tid, {k}, :tv__{k})")
    rhs = "NULL" if value is None else (":tv__json" if ftype is FieldType.LIST else ":tv")
    if tx.write == "insert":
        return [f"INSERT INTO {t} (id, {f}) VALUES (:tid, {rhs})"] + elements
    return [f"UPDATE {t} SET {f} = {rhs} WHERE id = :tid"] + elements


def _doc(tx: TxWrite, schema: Schema) -> list:
    t, f = tx.table, tx.field
    key = f"WHERE {document.path('id')} = CAST(:tid AS NVARCHAR(256))"
    if tx.write == "delete":
        return [f"DELETE FROM {t} {key}"]
    if tx.write == "insert":
        return [f"INSERT INTO {t} VALUES(:txdoc)"]
    value = None if tx.remove else tx.tv
    if value is None:
        return [f"UPDATE {t} UNSET {document.path(f)} {key}"]
    return [f"UPDATE {t} SET {document.path(f)} = PARSE_JSON(:tv__json) {key}"]


def _rdf(tx: TxWrite, schema: Schema) -> list:
    t, f = tx.table, tx.field
    n = record_iri(tx.tid)
    g = GRAPH_TOKEN
    if tx.write == "delete":
        return [f"{PREFIX_TEXT} DELETE WHERE {{ GRAPH {g} {{ {n} ?p ?o }} }}"]
    value = None if tx.remove else tx.tv
    values = []
    if value is not None:
        values.append(f"{n} p:{f} {sparql_literal(value)}")
        if isinstance(value, list):
            values += [f"{n} i:{f} {sparql_literal(x)}" for x in sorted(set(value))]
    if tx.write == "insert":
        from ganda.hana.mapping.rdf import class_triples

        triples = class_triples(n, t, schema) + [f"{n} p:id {sparql_string(tx.tid)}"] + values
        return [f"{PREFIX_TEXT} INSERT DATA {{ GRAPH {g} {{ {' . '.join(triples)} }} }}"]
    out = [f"{PREFIX_TEXT} DELETE WHERE {{ GRAPH {g} {{ {n} p:{f} ?o }} }}"]
    if _field(schema, tx).type is FieldType.LIST:
        out.append(f"{PREFIX_TEXT} DELETE WHERE {{ GRAPH {g} {{ {n} i:{f} ?o }} }}")
    if values:
        out.append(f"{PREFIX_TEXT} INSERT DATA {{ GRAPH {g} {{ {' . '.join(values)} }} }}")
    return out


def statements(tx: TxWrite, schema: Schema, data_model: str) -> list:
    # the graph data model's door is the rel one: its tables are the rel tables, and the
    # workspace reads the transaction's own writes (RULES.md, G12)
    return {"rel": _rel, "doc": _doc, "rdf": _rdf, "graph": _rel}[data_model](tx, schema)
