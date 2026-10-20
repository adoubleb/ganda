"""ArangoDB's physical design: the index kinds a schema may draw and what each one
enforces, the id index axis, the write forms a setup may take, the value pools, and how
an index keys a value. The vocabulary is `ganda.arangodb.storage.dialect`'s.

An index is a persistent (RocksDB) index on one or two attributes, unique or not,
sparse (`IndexSpec.ignore_nulls`) or not; a LIST field's index is the expanded array
index (`f[*]`); a MAP field's `BY KEY` / `BY VALUE` index has no equivalent and is not
created (the spec stays in the schema, inert). Every data model indexes node documents
alike; an edge index lands on the edge collection (graph) or the junction (rel).
"""
from __future__ import annotations

import random

from ganda.core.data.generator import BOUNDARY_VALUES as _CORE_BOUNDARY_VALUES
from ganda.core.data.model import FieldType, WriteForm
from ganda.core.rng import stream
from ganda.arangodb.storage.dialect import (
    EDGE_UPDATE_FORMS,
    ID_INDEX_KINDS,
    INSERT_FORMS,
    TRANSACTION_FORMS,
    UNIQUE_KINDS,
    UPDATE_FORMS,
    is_valid_key,  # noqa: F401
)

_INDEX_KIND_WEIGHTS = {"persistent": 75, "unique": 25}
# the kinds a composite, an edge or a list index draws from
NON_UNIQUE_KINDS = ("persistent",)
# the doc data model embeds its relationships: no collection to index
EDGE_INDEX_DATA_MODELS = ("graph", "rel", "both")
_DEMOTED = {"unique": "persistent"}


def _signed_zero(v) -> bool:
    return isinstance(v, float) and v == 0 and str(v).startswith("-")


def _boundary_values() -> dict:
    """The core's pools without a signed zero: the document API stores `-0.0` with its
    sign and every AQL write path stores `0` (the legacy port's A2), and a stored
    `-0.0` is one value to comparison and two to hashing (A1) -- a divergence of the
    WRITE DOOR drawn, not of the storage model, so it stays out of the data."""
    out = {}
    for ftype, pool in _CORE_BOUNDARY_VALUES.items():
        out[ftype] = [v for v in pool if not _signed_zero(v)]
    return out


BOUNDARY_VALUES = _boundary_values()


def draw_index_kind(rng: random.Random) -> str:
    (kind,) = rng.choices(list(_INDEX_KIND_WEIGHTS), weights=list(_INDEX_KIND_WEIGHTS.values()))
    return kind


def is_unique(ix) -> bool:
    """A second record with the same key is REJECTED (errorNum 1210)."""
    return ix.kind in UNIQUE_KINDS


def holds_one_record_per_key(ix) -> bool:
    return ix.kind in UNIQUE_KINDS


def demoted_kind(kind: str) -> str:
    return _DEMOTED.get(kind, kind)


def index_key_value(v, ci: bool = False):
    """A value as an index keys it, hashable: two equal numbers are one key whatever
    their JSON form (`1` and `1.0`), a list its elements (an expanded index keys each,
    but two records holding the same list hold the same keys), a map its items. The
    collation flag plays no part: ArangoDB compares strings code point by code point."""
    if isinstance(v, bool):
        return ("b", v)
    if isinstance(v, (int, float)):
        return ("n", v)
    if isinstance(v, list):
        return tuple(index_key_value(x) for x in v)
    if isinstance(v, dict):
        return tuple(sorted((k, index_key_value(x)) for k, x in v.items()))
    return v


def draw_id_indexes(seed: "int | None", cfg) -> dict:
    """data model -> the kind of index on `id` (`ID_INDEX_KINDS`), each data model on
    its own tail stream; a data model left out keeps the unique default."""
    id_index: dict = {}
    if seed is not None and cfg.id_index_probability > 0:
        for data_model in ("doc", "graph", "rel"):
            r = stream(seed, "idindex", data_model)
            if r.random() < cfg.id_index_probability:
                id_index[data_model] = r.choice(ID_INDEX_KINDS)
    return id_index


def draw_storage_options(seed: "int | None", cfg, node_types: tuple) -> tuple:
    """No storage option: a single server holds a collection in one shard."""
    return node_types


def draw_write_form(rng: random.Random) -> WriteForm:
    """How a setup writes: the insert door, the NodeUpdate form, the transaction door, a
    cancelled transaction of junk writes, the relationship update form."""
    return WriteForm(
        insert_via=rng.choice(INSERT_FORMS),
        update_form=rng.choice(UPDATE_FORMS),
        transaction=rng.choice(TRANSACTION_FORMS),
        cancelled_junk=rng.random() < 0.5,
        edge_update_form=rng.choice(EDGE_UPDATE_FORMS),
    )
