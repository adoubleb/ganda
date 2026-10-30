"""The building blocks of the SELECT and PATH syntaxes: the method catalog with its
Python mirrors, the transaction door's statements, and the core's shared helpers."""
from __future__ import annotations

import math

from ganda.core.query.generators.support import bfs_distances, composite_and as _composite_and, draw_tx, tx_binds, tx_rows  # noqa: F401
from ganda.core.query.ir.select import TxWrite
from ganda.core.data.model import FieldType, Schema
from ganda.arangodb.rules import assumes
from ganda.arangodb.rules.catalog import REFUSED, TRANSACTION
from ganda.arangodb.engine import tx as _tx
from ganda.arangodb.engine.semantics import F64_INT_EXACT
from ganda.arangodb.engine.dialect import document_key


def _bmp(s: str) -> bool:
    return all(ord(c) < 0x10000 for c in s)


def _small(v) -> bool:
    return abs(v) < F64_INT_EXACT


# methods over present values: casing over ASCII, character positions over the BMP
# (an astral code point is two UTF-16 units to some functions); no float as text, no rounding
_INT_METHODS = [
    ("TO_STRING({f})", _small),
    ("{f} + 1", _small),
    ("{f} - 1", _small),
    ("{f} * 2", lambda v: abs(v) < 2**52),
    ("{f} / 2", _small),
    ("{f} % 3", _small),
    ("ABS({f})", _small),
    ("FLOOR({f})", _small),
]
_FLOAT_METHODS = [
    ("{f} * 2", lambda v: math.isfinite(v) and abs(v) < 8e307),
    ("{f} + 0", lambda v: True),
    ("ABS({f})", lambda v: True),
    ("FLOOR({f})", lambda v: abs(v) < 2**62),
]
_DATE_METHODS = [
    ("LENGTH({f})", lambda v: True),
    ("LEFT({f}, 10)", lambda v: True),
    ("SUBSTRING({f}, 11)", lambda v: True),
    ("CONCAT({f}, 'x')", lambda v: True),
]
METHODS = {
    FieldType.STRING: [
        ("UPPER({f})", lambda s: s.isascii()),
        ("LOWER({f})", lambda s: s.isascii()),
        ("LENGTH({f})", lambda s: True),
        ("CONCAT({f}, 'x')", lambda s: True),
        ("CONCAT('x', {f})", lambda s: True),
        ("LEFT({f}, 1)", _bmp),
        ("TRIM({f})", lambda s: s.isascii()),
        ("CONTAINS({f}, 'a')", lambda s: True),
        ("FIND_FIRST({f}, 'a')", _bmp),
    ],
    FieldType.INT: _INT_METHODS,
    FieldType.INT32: _INT_METHODS,
    FieldType.FLOAT: _FLOAT_METHODS,
    FieldType.FLOAT32: _FLOAT_METHODS,
    FieldType.DECIMAL: _FLOAT_METHODS,
    FieldType.BOOL: [
        ("TO_STRING({f})", lambda v: True),
        ("TO_NUMBER({f})", lambda v: True),
        ("NOT {f}", lambda v: True),
    ],
    FieldType.LIST: [
        ("LENGTH({f})", lambda v: True),
        ("LENGTH(UNIQUE({f}))", lambda v: True),
        ("FIRST({f})", lambda v: True),
        ("MAX({f})", lambda v: True),
        ("SUM({f})", lambda v: sum(abs(x) for x in v) < F64_INT_EXACT),
    ],
    FieldType.MAP: [
        ("LENGTH({f})", lambda v: True),
        ("LENGTH(ATTRIBUTES({f}))", lambda v: True),
        ("HAS({f}, 'k1')", lambda v: True),
    ],
    FieldType.DATETIME: _DATE_METHODS,
    FieldType.DATE: _DATE_METHODS,
    # a blob is projected, never computed over
    FieldType.BINARY: [],
}


# --- the transaction door ---------------------------------------------------------------------


def tx_statements(tx: TxWrite, schema: Schema, data_model: str) -> list:
    """The door's write on `data_model` (the same AQL on every one: a node document is
    the same document everywhere): a record removed, inserted with one field, or
    updated (a field set, set to null, or unset)."""
    t, f = tx.table, tx.field
    if tx.write == "delete":
        return [f"FOR x IN {t} FILTER x.id == @tid REMOVE x IN {t}"]
    if tx.write == "insert":
        return [f"INSERT {{_key: @txkey, id: @tid, {f}: @tv}} INTO {t}"]
    if tx.remove:
        return [f"FOR x IN {t} FILTER x.id == @tid UPDATE x WITH {{{f}: null}} IN {t} OPTIONS {{keepNull: false, mergeObjects: false}}"]
    return [f"FOR x IN {t} FILTER x.id == @tid UPDATE x WITH {{{f}: @tv}} IN {t} OPTIONS {{mergeObjects: false}}"]


def tx_extra_binds(tx: TxWrite) -> dict:
    return {"txkey": document_key(tx.tid.split("/", 1)[1])} if tx.write == "insert" else {}


# the door's question sees the door's own write; a transaction naming a collection the
# database lacks is refused (the doc data model has no edge collections)
assumes("transaction.own_uncommitted_visible", TRANSACTION, True)
assumes("accepts.transaction.collection_absent_from_database", TRANSACTION, REFUSED)


def tx_script(tx: TxWrite, schema: Schema, data_model: str, question: str) -> str:
    """The door text: the transaction over the written class (read on every
    collection the question may touch), the write, the question."""
    read = [nt.name for nt in schema.node_types] + ([et.name for et in schema.edge_types] if data_model != "doc" else [])
    return _tx.script({"write": [tx.table], "read": read}, tx_statements(tx, schema, data_model), question)


def composite_and(rng, fields, values_of):
    from ganda.arangodb.provider import PROVIDER

    return _composite_and(rng, fields, values_of, PROVIDER)


def base_union(schema, var: str) -> str:
    """The base class's records, every node collection's in turn: `UNION((FOR v_t IN t
    RETURN v_t), ...)`, or the one collection's subquery (`UNION()` takes two arguments
    at least: a base class over one class would be refused, errorNum 1541)."""
    parts = [f"(FOR {var}_{nt.name} IN {nt.name} RETURN {var}_{nt.name})" for nt in schema.node_types]
    return parts[0] if len(parts) == 1 else f"UNION({', '.join(parts)})"
