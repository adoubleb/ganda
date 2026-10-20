"""The building blocks of the SELECT and PATH syntaxes: the method catalog with its
Python mirrors, the transaction door's statements, and the core's shared helpers."""
from __future__ import annotations

import math

from ganda.core.query.generators.support import bfs_distances, composite_and as _composite_and, draw_tx, tx_binds, tx_rows  # noqa: F401
from ganda.core.query.ir.select import TxWrite
from ganda.core.data.model import FieldType, Schema
from ganda.arangodb.engine import tx as _tx
from ganda.arangodb.storage.dialect import document_key

F64_INT_EXACT = 2**53


def _c_mod(a: int, b: int) -> int:
    """AQL `%`: the remainder takes the sign of the dividend (C's `fmod`)."""
    q = abs(a) // abs(b)
    q = q if (a >= 0) == (b >= 0) else -q
    return a - q * b


def _bmp(s: str) -> bool:
    return all(ord(c) < 0x10000 for c in s)


def _small(v) -> bool:
    return abs(v) < F64_INT_EXACT


# Methods with an exact Python mirror over PRESENT values (the query filters `!= null`).
# Casing and trimming mirrors are trusted over ASCII only; the character-position
# functions over the Basic Multilingual Plane (an astral code point is two UTF-16 units
# to some of the engine's string functions, A11's class). Nothing renders a float as
# text (its shortest form is the engine's business) and nothing rounds (A5).
_INT_METHODS = [
    ("TO_STRING({f})", lambda v: str(v), _small),
    ("{f} + 1", lambda v: v + 1, _small),
    ("{f} - 1", lambda v: v - 1, _small),
    ("{f} * 2", lambda v: v * 2, lambda v: abs(v) < 2**52),
    ("{f} / 2", lambda v: v / 2, _small),
    ("{f} % 3", lambda v: _c_mod(v, 3), _small),
    ("ABS({f})", lambda v: abs(v), _small),
    ("FLOOR({f})", lambda v: v, _small),
]
_FLOAT_METHODS = [
    ("{f} * 2", lambda v: v * 2, lambda v: math.isfinite(v) and abs(v) < 8e307),
    ("{f} + 0", lambda v: v + 0, lambda v: True),
    ("ABS({f})", lambda v: abs(v), lambda v: True),
    ("FLOOR({f})", lambda v: math.floor(v), lambda v: abs(v) < 2**62),
]
_DATE_METHODS = [
    ("LENGTH({f})", lambda v: len(v), lambda v: True),
    ("LEFT({f}, 10)", lambda v: v[:10], lambda v: True),
    ("SUBSTRING({f}, 11)", lambda v: v[11:], lambda v: True),
    ("CONCAT({f}, 'x')", lambda v: v + "x", lambda v: True),
]
METHODS = {
    FieldType.STRING: [
        ("UPPER({f})", lambda s: s.upper(), lambda s: s.isascii()),
        ("LOWER({f})", lambda s: s.lower(), lambda s: s.isascii()),
        ("LENGTH({f})", lambda s: len(s), lambda s: True),
        ("CONCAT({f}, 'x')", lambda s: s + "x", lambda s: True),
        ("CONCAT('x', {f})", lambda s: "x" + s, lambda s: True),
        ("LEFT({f}, 1)", lambda s: s[:1], _bmp),
        ("TRIM({f})", lambda s: s.strip(" \t\n\r"), lambda s: s.isascii()),
        ("CONTAINS({f}, 'a')", lambda s: "a" in s, lambda s: True),
        ("FIND_FIRST({f}, 'a')", lambda s: s.find("a"), _bmp),
    ],
    FieldType.INT: _INT_METHODS,
    FieldType.INT32: _INT_METHODS,
    FieldType.FLOAT: _FLOAT_METHODS,
    FieldType.FLOAT32: _FLOAT_METHODS,
    FieldType.DECIMAL: _FLOAT_METHODS,
    FieldType.BOOL: [
        ("TO_STRING({f})", lambda v: "true" if v else "false", lambda v: True),
        ("TO_NUMBER({f})", lambda v: 1 if v else 0, lambda v: True),
        ("NOT {f}", lambda v: not v, lambda v: True),
    ],
    FieldType.LIST: [
        ("LENGTH({f})", lambda v: len(v), lambda v: True),
        ("LENGTH(UNIQUE({f}))", lambda v: len(set(v)), lambda v: True),
        ("FIRST({f})", lambda v: v[0] if v else None, lambda v: True),
        ("MAX({f})", lambda v: max(v) if v else None, lambda v: True),
        ("SUM({f})", lambda v: sum(v), lambda v: sum(abs(x) for x in v) < F64_INT_EXACT),
    ],
    FieldType.MAP: [
        ("LENGTH({f})", lambda v: len(v), lambda v: True),
        ("LENGTH(ATTRIBUTES({f}))", lambda v: len(v), lambda v: True),
        ("HAS({f}, 'k1')", lambda v: "k1" in v, lambda v: True),
    ],
    FieldType.DATETIME: _DATE_METHODS,
    FieldType.DATE: _DATE_METHODS,
    # a blob is projected, never computed over
    FieldType.BINARY: [],
}


def method_fn(template: str, ftype: FieldType):
    for t, fn, _ in METHODS[ftype]:
        if t == template:
            return fn
    raise KeyError(template)


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


def tx_script(tx: TxWrite, schema: Schema, data_model: str, question: str) -> str:
    """The door text: the transaction over the written class (read on every
    collection the question may touch), the write, the question."""
    read = [nt.name for nt in schema.node_types] + ([et.name for et in schema.edge_types] if data_model != "doc" else [])
    return _tx.script({"write": [tx.table], "read": read}, tx_statements(tx, schema, data_model), question)


def composite_and(rng, fields, values_of):
    from ganda.arangodb.provider import PROVIDER

    return _composite_and(rng, fields, values_of, PROVIDER)
