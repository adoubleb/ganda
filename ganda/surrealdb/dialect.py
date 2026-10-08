"""SurrealDB's vocabulary for the model's abstract types and physical-design knobs.

Taken from the legacy port (`surrealDB/fuzzer/model.py`, `materialize/common.py`); the
provider's mapping and concrete syntax will read these.
"""
from __future__ import annotations

from ganda.core.model import FieldType

# FieldType -> the `DEFINE FIELD ... TYPE` name
SURREAL_TYPES = {
    FieldType.INT: "int",
    FieldType.FLOAT: "float",
    FieldType.STRING: "string",
    FieldType.BOOL: "bool",
    FieldType.LIST: "array<int>",
    FieldType.DECIMAL: "decimal",
    FieldType.DATETIME: "datetime",
    FieldType.MAP: "object",
    FieldType.BINARY: "bytes",  # the model's base64 text decoded on write, encoded on read
}

# IndexSpec.kind values this engine defines
INDEX_KINDS = ("standard", "unique", "search", "hnsw")
# how a relationship is stored per data model
DATA_MODELS = {
    "doc": "an array of link objects on the source record: `knows: [{target: person:r2, weight: 3}]`",
    "graph": "`RELATE` edge tables",
    "rel": "a junction table with `src` / `dst` record links",
}
