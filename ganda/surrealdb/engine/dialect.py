"""SurrealDB's vocabulary for the model's abstract types, its record ids and its literals.

A record is `table:key`: the logical id `n1/r0` of the core is the record id `n1:r0`, a
custom key written in backticks when it is not a bare word (`n1:`hello world``), which is
how the engine prints it back. SurrealQL takes constants as typed literals (`d"..."`,
`1.5dec`, `b"00ff"`); `surql_literal` renders a model value for a column of a type.
"""
from __future__ import annotations

import base64
import json
import re

from ganda.core.data.model import DATE_TYPES, FieldType

# FieldType -> the `DEFINE FIELD ... TYPE` kind (every column is `option<kind | null>`:
# a row may omit a field, and an explicit stored null is NULL)
SURREAL_TYPES = {
    FieldType.INT: "int",
    FieldType.INT32: "int",  # the engine has one 64-bit integer
    FieldType.FLOAT: "float",
    FieldType.FLOAT32: "float",  # and one 64-bit float
    FieldType.STRING: "string",
    FieldType.BOOL: "bool",
    FieldType.LIST: "array<int>",
    FieldType.DECIMAL: "decimal",
    FieldType.DATETIME: "datetime",
    FieldType.DATE: "datetime",
    FieldType.MAP: "object",
    FieldType.BINARY: "bytes",  # the model's base64 text, decoded on write, read back as a byte list
}

# the index kinds a schema may draw: a plain index, or one that rejects a second record per key
INDEX_KINDS = ("standard", "unique")
UNIQUE_KINDS = ("unique",)

_BARE_KEY = re.compile(r"^[A-Za-z0-9_]+$")
# what a custom key may not hold: the quoting characters (an all-digit key is written
# in backticks, which keeps it a string key)
_KEY_FORBIDDEN = set("`\\⟨⟩\n\r\t\x00")


def is_valid_key(key: str) -> bool:
    return bool(key) and not (_KEY_FORBIDDEN & set(key))


def render_id_key(key: str) -> str:
    """A record key as SurrealQL spells it: bare when a word, else in backticks."""
    if _BARE_KEY.match(key) and not key.isdigit():
        return key
    return "`" + key.replace("\\", "\\\\").replace("`", "\\`") + "`"


def engine_id(logical: str) -> str:
    """`n1/r0` -> `n1:r0`."""
    table, key = logical.split("/", 1)
    return f"{table}:{render_id_key(key)}"


def logical_id(text) -> "str | None":
    """`n1:r0` / `n1:`hello world`` / `n1:⟨hello world⟩` -> `n1/r0`; None for anything
    that is not a record id's text."""
    if not isinstance(text, str) or ":" not in text:
        return None
    table, key = text.split(":", 1)
    if not _BARE_KEY.match(table):
        return None
    if len(key) >= 2 and key[0] == "`" and key[-1] == "`":
        key = key[1:-1].replace("\\`", "`").replace("\\\\", "\\")
    elif len(key) >= 2 and key[0] == "⟨" and key[-1] == "⟩":
        key = key[1:-1].replace("\\⟩", "⟩").replace("\\\\", "\\")
    return f"{table}/{key}"


def iso_datetime(v: str) -> str:
    """The core's `yyyy-MM-dd HH:mm:ss` (UTC) as the engine's datetime text."""
    return v.replace(" ", "T") + "Z"


def surql_literal(v, ftype: "FieldType | None" = None) -> str:
    """A model value as a SurrealQL literal for a column of `ftype` (None: the value's
    own kind -- a bind variable's)."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if ftype in DATE_TYPES and isinstance(v, str):
        return f'd"{iso_datetime(v)}"'
    if ftype is FieldType.DECIMAL and isinstance(v, (int, float)):
        return f"{v!r}dec"
    if ftype is FieldType.BINARY and isinstance(v, str):
        return 'b"' + base64.b64decode(v).hex() + '"'
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        # raw UTF-8 (the engine takes no `\uXXXX` surrogate escape)
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        # the elements of a constant list are typed as the column is (an `IN` list of
        # decimals); a LIST column's elements are integers either way
        return "[" + ", ".join(surql_literal(x, ftype) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k}: {surql_literal(x)}" for k, x in v.items()) + " }"
    raise TypeError(f"no SurrealQL literal for {type(v).__name__}: {v!r}")
