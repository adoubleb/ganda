"""ArangoDB's vocabulary for the model's abstract types and physical-design knobs.

ArangoDB documents are schemaless JSON (VelocyPack): a `FieldType` is the JSON type its
values carry, so there is nothing to declare per column. What the provider decides is
how a record is ADDRESSED: every document carries the model's logical id in its `id`
attribute (`n1/r0`), and its engine key `_key` is that id's key percent-encoded into
the engine's key alphabet (`document_key`), so the engine id `_id` is a deterministic
function of the logical id (`engine_id`) and its inverse (`logical_id`) needs no lookup.
The three data models store a relationship differently (`DATA_MODELS`).
"""
from __future__ import annotations

import re
from urllib.parse import quote, unquote

from ganda.core.data.model import FieldType

# the JSON type a value of each model type carries (what `TYPENAME()` answers), which
# AQL's order across types ranks: null < bool < number < string < array < object
AQL_TYPENAMES = {
    FieldType.INT: "number",
    FieldType.INT32: "number",
    FieldType.FLOAT: "number",
    FieldType.FLOAT32: "number",
    FieldType.DECIMAL: "number",
    FieldType.STRING: "string",
    FieldType.DATETIME: "string",  # the model's `yyyy-MM-dd HH:mm:ss` text; AQL has no date type
    FieldType.DATE: "string",
    FieldType.BOOL: "bool",
    FieldType.LIST: "array",
    FieldType.MAP: "object",
    FieldType.BINARY: "string",  # JSON has no byte type: the base64 text is stored as it is
}
TYPE_ORDER = ("null", "bool", "number", "string", "array", "object")

# `IndexSpec.kind`: a persistent (RocksDB) index, non-unique or unique; `ignore_nulls`
# is the index's `sparse` flag (a sparse index holds no entry for a null or absent key)
INDEX_KINDS = ("persistent", "unique")
UNIQUE_KINDS = ("unique",)
# the index on the logical `id` attribute: the model's default "UNIQUE" is a unique
# persistent index; the axis may make it a plain one (`persistent`). Never none: a hop
# of the relational data model is a lookup by `id`, which no large instance can scan
ID_INDEX_KINDS = ("unique", "persistent")
# the id index when the id index axis drew none for a data model
DEFAULT_ID_INDEX = "unique"
# the write doors (`WriteForm`): the document API's bulk insert or `FOR d IN @docs
# INSERT d`; a NodeUpdate as UPDATE / REPLACE / UPSERT / `INSERT ... overwriteMode` /
# PATCH / PUT; a relationship's update as UPDATE or REPLACE; the data steps inside one
# stream transaction or auto-committed
INSERT_FORMS = ("api", "aql")
UPDATE_FORMS = ("update", "replace", "upsert", "insert_overwrite", "patch_api", "put_api")
EDGE_UPDATE_FORMS = ("update", "replace")
TRANSACTION_FORMS = ("none", "tx")
# how a relationship is stored per data model
DATA_MODELS = {
    "doc": "an embedded array on the source document: `e1: [{eid: 'e1/e0', target: 'n2/r3', w: 3}]` (`target` the engine id)",
    "graph": "an edge collection (`_from` / `_to` the engine ids), traversed natively",
    "rel": "a plain collection with `src_id` / `dst_id` holding the LOGICAL ids, a persistent index on each",
}

# ArangoDB document keys: letters, digits and `_ - : . @ ( ) + , = ; $ ! * ' %`, up to 254
# bytes, never empty (documented; probed on 3.12 by the legacy port: `/`, non-ASCII, a
# space, "" and 255 characters are "illegal document key")
ARANGO_KEY_RE = re.compile(r"^[A-Za-z0-9_\-:.@()+,=;$!*'%]{1,254}$")
# the characters `document_key` keeps as they are: the key alphabet without `%`, which
# is the escape (so an encoded key decodes to exactly one logical key)
_KEY_SAFE = "_-:.@()+,=;$!*'"


def is_valid_key(key: str) -> bool:
    """Whether the model may use `key` as a record's key: any non-empty text without a
    `/` (the id separator), since the engine key is its percent-encoding."""
    return bool(key) and "/" not in key


def document_key(key: str) -> str:
    """The engine `_key` of a logical key: the key itself when it is in the engine's
    alphabet, else percent-encoded into it (`a b` -> `a%20b`, `%` -> `%25`, a
    non-ASCII character its UTF-8 bytes); `~`, which `quote` never escapes, is."""
    out = quote(key, safe=_KEY_SAFE).replace("~", "%7E")
    assert ARANGO_KEY_RE.match(out), key
    return out


def engine_id(record_id: str) -> str:
    """`n1/a b` -> `n1/a%20b`: the `_id` the engine assigns the record."""
    coll, key = record_id.split("/", 1)
    return f"{coll}/{document_key(key)}"


def logical_id(engine_id_: str) -> str:
    coll, key = engine_id_.split("/", 1)
    return f"{coll}/{unquote(key)}"
