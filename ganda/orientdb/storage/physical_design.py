"""OrientDB's physical design: the index kinds a schema may draw and what each one
enforces, the id index and cluster axes, the write forms a setup may take, and how an
index keys a value. The vocabulary is `ganda.orientdb.storage.dialect`'s.
"""
from __future__ import annotations

import dataclasses
import random

from ganda.core.data.generator import BOUNDARY_VALUES  # noqa: F401 -- every one probed to round-trip on 3.2.56
from ganda.core.data.model import FieldType, WriteForm, options
from ganda.core.rng import stream
from ganda.orientdb.storage.dialect import (
    EDGE_FROM_FORMS,
    EDGE_UPDATE_FORMS,
    ID_INDEX_KINDS,
    INSERT_FORMS,
    TRANSACTION_FORMS,
    UNIQUE_KINDS,
    UPDATE_FORMS,
    is_valid_key,  # noqa: F401
)

_INDEX_KIND_WEIGHTS = {"NOTUNIQUE": 40, "UNIQUE": 10, "NOTUNIQUE_HASH_INDEX": 20, "UNIQUE_HASH_INDEX": 5, "DICTIONARY": 15}
# the kinds a composite, an edge or a list index draws from
NON_UNIQUE_KINDS = ("NOTUNIQUE", "NOTUNIQUE_HASH_INDEX")
# the doc data model embeds its relationships: no class to index
EDGE_INDEX_DATA_MODELS = ("graph", "rel", "both")
_DEMOTED = {"UNIQUE": "NOTUNIQUE", "UNIQUE_HASH_INDEX": "NOTUNIQUE_HASH_INDEX", "DICTIONARY": "NOTUNIQUE"}


def draw_index_kind(rng: random.Random) -> str:
    (kind,) = rng.choices(list(_INDEX_KIND_WEIGHTS), weights=list(_INDEX_KIND_WEIGHTS.values()))
    return kind


def is_unique(ix) -> bool:
    """A second record with the same key is REJECTED."""
    return ix.kind in UNIQUE_KINDS


def holds_one_record_per_key(ix) -> bool:
    """One record per key, by rejection (unique) or by replacement (DICTIONARY)."""
    return ix.kind in UNIQUE_KINDS + ("DICTIONARY",)


def demoted_kind(kind: str) -> str:
    """The kind an index whose keys collide falls back to: a unique kind's NOTUNIQUE
    sibling; a DICTIONARY index over duplicate values becomes NOTUNIQUE (the planner
    would answer an equality predicate through it and drop rows, O4)."""
    return _DEMOTED[kind]


def index_key_value(v, ci: bool = False):
    """A value as an index keys it, hashable."""
    if isinstance(v, bool):
        return ("b", v)
    if isinstance(v, float):
        return ("f", v, str(v)[0] == "-")
    if isinstance(v, list):
        return tuple(index_key_value(x) for x in v)
    if isinstance(v, dict):
        return tuple(sorted((k, index_key_value(x)) for k, x in v.items()))
    if ci and isinstance(v, str):
        # a ci index stores the lower-cased key (probed: a UNIQUE index rejects `A`
        # next to `a`)
        return v.lower()
    return v


def draw_id_indexes(seed: "int | None", cfg) -> dict:
    """data model -> the kind of index on `id` (`ID_INDEX_KINDS`), each data model on
    its own tail stream; a data model left out keeps the UNIQUE default."""
    id_index: dict = {}
    if seed is not None and cfg.id_index_probability > 0:
        for data_model in ("doc", "graph", "rel"):
            r = stream(seed, "idindex", data_model)
            if r.random() < cfg.id_index_probability:
                id_index[data_model] = r.choice(ID_INDEX_KINDS)
    return id_index


def draw_index_options(rng: random.Random, ftype: FieldType) -> tuple:
    """A map index keys either the map's keys or its values (`by`: KEY / VALUE)."""
    return options(by=rng.choice(("KEY", "VALUE"))) if ftype is FieldType.MAP else ()


def draw_storage_options(seed: "int | None", cfg, node_types: tuple) -> tuple:
    """The clusters axis: a class spread over 2 or 3 physical clusters."""
    if seed is None or cfg.clusters_probability <= 0:
        return node_types
    out = []
    for nt in node_types:
        r = stream(seed, "clusters", nt.name)
        if r.random() < cfg.clusters_probability:
            nt = dataclasses.replace(nt, options=options(**dict(nt.options), clusters=r.choice((2, 3))))
        out.append(nt)
    return tuple(out)


def draw_write_form(rng: random.Random) -> WriteForm:
    form = WriteForm(
        insert_via=rng.choice(INSERT_FORMS),
        update_form=rng.choice(UPDATE_FORMS[:4]),
        edge_from=rng.choice(EDGE_FROM_FORMS),
        transaction=rng.choice(TRANSACTION_FORMS),
        cancelled_junk=rng.random() < 0.5,
    )
    # the later doors draw AFTER the original ones, so an older seed keeps its forms
    if rng.random() < 0.25:
        form = dataclasses.replace(form, update_form="arith")
    return dataclasses.replace(form, edge_update_form=rng.choice(EDGE_UPDATE_FORMS), rebuild_indexes=rng.random() < 0.3)
