"""SurrealDB's physical design: the index kinds a schema may draw and what each one
enforces, the SCHEMAFULL axis, the write forms a setup may take, and how an index keys
a value. Type and index kind names are `ganda.surrealdb.engine.dialect`'s.
"""
from __future__ import annotations

import dataclasses
import random

from ganda.core.data.generator import BOUNDARY_VALUES  # noqa: F401 -- probed to round-trip on 3.3.4 (rules/probe.py)
from ganda.core.data.model import FieldType, WriteForm, options
from ganda.core.rng import stream
from ganda.surrealdb.engine.dialect import UNIQUE_KINDS, is_valid_key  # noqa: F401

DATA_MODELS = ("doc", "graph", "rel")

# Write forms (`WriteForm`): each reaches another engine write path to the same final
# state. insert: `CREATE .. SET` / `CREATE .. CONTENT` / `INSERT INTO t {..}`; update:
# SET / MERGE / CONTENT / PATCH; edge creation: `RELATE` or `INSERT RELATION`; data
# steps auto-committed or in `BEGIN .. COMMIT` transactions.
INSERT_FORMS = ("set", "content", "insert")
UPDATE_FORMS = ("set", "merge", "content", "patch")
EDGE_UPDATE_FORMS = ("set", "merge", "patch")
EDGE_FROM_FORMS = ("relate", "insert_relation")
TRANSACTION_FORMS = ("none", "tx")

_INDEX_KIND_WEIGHTS = {"standard": 75, "unique": 25}
# the kinds a composite, an edge or a list index draws from
NON_UNIQUE_KINDS = ("standard",)
# the doc data model embeds its relationships: an index over them would be an array
# index on the source table (`e1[*].w`), the shape of F4
EDGE_INDEX_DATA_MODELS = ("graph", "rel", "both")


def draw_index_kind(rng: random.Random) -> str:
    (kind,) = rng.choices(list(_INDEX_KIND_WEIGHTS), weights=list(_INDEX_KIND_WEIGHTS.values()))
    return kind


def is_unique(ix) -> bool:
    """A second record with the same key is REJECTED."""
    return ix.kind in UNIQUE_KINDS


def holds_one_record_per_key(ix) -> bool:
    return is_unique(ix)


def demoted_kind(kind: str) -> str:
    """The kind an index whose keys collide falls back to."""
    return "standard"


def index_key_value(v, ci: bool = False):
    """A value as an index keys it, hashable: numbers by value (`1` and `1.0` are one
    key), lists and maps structurally."""
    if isinstance(v, bool):
        return ("b", v)
    if isinstance(v, float):
        return ("n", int(v)) if v == int(v) else ("n", v)
    if isinstance(v, int):
        return ("n", v)
    if isinstance(v, list):
        return tuple(index_key_value(x) for x in v)
    if isinstance(v, dict):
        return tuple(sorted((k, index_key_value(x)) for k, x in v.items()))
    return v


def draw_id_indexes(seed: "int | None", cfg) -> dict:
    """The record id is the primary key on every data model: no id index is drawn."""
    return {}


def draw_index_options(rng: random.Random, ftype: FieldType) -> tuple:
    return ()


def draw_storage_options(seed: "int | None", cfg, node_types: tuple) -> tuple:
    """The SCHEMAFULL axis: per class and data model, on its own tail stream, the class
    (and the relationship tables leaving it) is declared with every field's type;
    `options(schemafull=(<data models>,))`. With `symmetric_physical_design` the first data
    model's draw holds on every data model."""
    if seed is None or cfg.schemafull_probability <= 0:
        return node_types
    out = []
    for nt in node_types:
        full = tuple(dm for dm in DATA_MODELS if stream(seed, "schemafull", nt.name, dm).random() < cfg.schemafull_probability)
        if cfg.symmetric_physical_design:
            full = DATA_MODELS if DATA_MODELS[0] in full else ()
        if full:
            nt = dataclasses.replace(nt, options=options(**dict(nt.options), schemafull=full))
        out.append(nt)
    return tuple(out)


def is_schemafull(nt, data_model: str) -> bool:
    return data_model in nt.option("schemafull", ())


def draw_write_form(rng: random.Random) -> WriteForm:
    return WriteForm(
        insert_via=rng.choice(INSERT_FORMS),
        update_form=rng.choice(UPDATE_FORMS),
        edge_from=rng.choice(EDGE_FROM_FORMS),
        transaction=rng.choice(TRANSACTION_FORMS),
        cancelled_junk=rng.random() < 0.5,
        edge_update_form=rng.choice(EDGE_UPDATE_FORMS),
        rebuild_indexes=rng.random() < 0.3,
    )
