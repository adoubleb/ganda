"""ArangoDB's vocabulary for the model's abstract types and physical-design knobs.

Taken from the legacy port (`arangoDB/fuzzer/model.py`); the provider's mapping and
concrete syntax will read these.
"""
from __future__ import annotations

from ganda.core.model import FieldType

# ArangoDB documents are schemaless JSON: a FieldType is the JSON type its values carry
# (what `TYPENAME()` answers), which the reference's cross-type order ranks
AQL_TYPENAMES = {
    FieldType.INT: "number",
    FieldType.FLOAT: "number",
    FieldType.STRING: "string",
    FieldType.BOOL: "bool",
    FieldType.LIST: "array",
    FieldType.MAP: "object",
}
# AQL's order across types (`compare_across_types` in the legacy reference)
TYPE_ORDER = ("null", "bool", "number", "string", "array", "object")
# IndexSpec.kind values this engine defines
INDEX_KINDS = ("persistent", "mdi", "inverted", "mdi-prefixed")
# how a relationship is stored per data model
DATA_MODELS = {
    "doc": "an embedded array on the source document: `knows: [{target: 'person/r2', weight: 3}]`",
    "graph": "an edge collection (`_from` / `_to`), optionally in a named graph",
    "rel": "a plain collection with `src` / `dst` keys and a persistent index on the source key",
}
