"""SAP HANA's physical design: the index kinds a schema may draw and what each enforces,
the id index and partition axes, the write forms a setup may take, the value pools,
and how an index keys a value. The vocabulary is `ganda.hana.storage.dialect`'s.

Indexes exist on the `rel` and `graph` data models (column tables: the graph's tables
are rel-shaped) and on the `doc` data model (collections); the triple store takes none,
so an index drawn for `rdf` alone is inert there -- its records are the same, which
keeps every index-vs-scan pair whole. The graph data model has one axis of its own: its
workspace's cache (`graph_cache`).
"""
from __future__ import annotations

import dataclasses
import random

from ganda.core.data.generator import BOUNDARY_VALUES as _CORE_BOUNDARY_VALUES
from ganda.core.data.model import FieldType, WriteForm
from ganda.core.rng import stream
from ganda.hana.storage.dialect import (
    EDGE_UPDATE_FORMS,
    GRAPH_CACHE_KEY,
    ID_INDEX_KINDS,
    INSERT_FORMS,
    SUPPORTED_TYPES,
    TRANSACTION_FORMS,
    UNIQUE_KINDS,
    UPDATE_FORMS,
    is_valid_key,  # noqa: F401
)

_INDEX_KIND_WEIGHTS = {"INDEX": 70, "UNIQUE": 30}
# the kinds a composite, an edge or a list index draws from: a column table's
# non-unique index is an INVERTED VALUE index (documented: INVERTED HASH and INVERTED
# INDIVIDUAL exist for composite UNIQUE constraints only)
NON_UNIQUE_KINDS = ("INDEX",)
# the doc data model embeds its relationships (no collection to index) and the triple
# store takes no index
EDGE_INDEX_DATA_MODELS = ("rel", "graph")
# the data models whose `id` index the axis may change (rdf takes none)
ID_INDEX_DATA_MODELS = ("doc", "rel", "graph")
_DEMOTED = {"UNIQUE": "INDEX"}


def _astral(v) -> bool:
    return isinstance(v, str) and any(ord(c) > 0xFFFF for c in v)


def _boundary_values() -> dict:
    """The core's adversarial pools for the types the provider models, without the
    values the three data models cannot hold alike yet: an astral code point (UTF-16
    order in SQL, code-point order in SPARQL), `-0.0` (one value with `0.0` in SQL, a
    different RDF term under SPARQL's DISTINCT and GROUP BY) and the subnormal doubles
    (to probe: whether every store keeps them)."""
    out = {}
    for ftype in SUPPORTED_TYPES:
        pool = _CORE_BOUNDARY_VALUES[ftype]
        if ftype is FieldType.STRING:
            pool = [v for v in pool if not _astral(v)]
        elif ftype is FieldType.FLOAT:
            pool = [v for v in pool if not (v == 0 and str(v).startswith("-")) and not (v != 0 and abs(v) < 2.2250738585072014e-308)]
        out[ftype] = pool
    return out


BOUNDARY_VALUES = _boundary_values()


def draw_index_kind(rng: random.Random) -> str:
    (kind,) = rng.choices(list(_INDEX_KIND_WEIGHTS), weights=list(_INDEX_KIND_WEIGHTS.values()))
    return kind


def is_unique(ix) -> bool:
    """A second record with the same key is REJECTED."""
    return ix.kind in UNIQUE_KINDS


def holds_one_record_per_key(ix) -> bool:
    return ix.kind in UNIQUE_KINDS


def demoted_kind(kind: str) -> str:
    return _DEMOTED.get(kind, kind)


def index_key_value(v, ci: bool = False):
    """A value as an index keys it, hashable (a SQL NULL is no key: the caller skips
    it; two equal numbers are one key whatever their Python type)."""
    if isinstance(v, bool):
        return ("b", v)
    if isinstance(v, (int, float)):
        return ("n", v)
    if isinstance(v, list):
        return tuple(index_key_value(x) for x in v)
    return v


def draw_id_indexes(seed: "int | None", cfg) -> dict:
    """data model -> the kind of index on `id` (`ID_INDEX_KINDS`), each on its own tail
    stream; a data model left out keeps the default (rel, graph: the PRIMARY KEY; doc:
    none). The graph workspace's cache axis rides in the same dict under
    `GRAPH_CACHE_KEY` (its own stream and probability): `"none"` for NO CACHE."""
    id_index: dict = {}
    if seed is not None and cfg.id_index_probability > 0:
        for data_model in ID_INDEX_DATA_MODELS:
            r = stream(seed, "idindex", data_model)
            if r.random() < cfg.id_index_probability:
                id_index[data_model] = r.choice(ID_INDEX_KINDS)
    if seed is not None and getattr(cfg, "graph_cache_probability", 0) > 0:
        r = stream(seed, "graphcache")
        if r.random() < cfg.graph_cache_probability:
            id_index[GRAPH_CACHE_KEY] = "none"
    return id_index


def graph_cache(schema) -> str:
    """The graph workspace's cache form (`dialect.GRAPH_CACHE_FORMS`): dynamic unless the
    axis drew NO CACHE."""
    return schema.id_index.get(GRAPH_CACHE_KEY, "dynamic")


def draw_storage_options(seed: "int | None", cfg, node_types: tuple) -> tuple:
    """The partition axis: a class's column table hash-partitioned into 2 or 3 parts."""
    if seed is None or cfg.clusters_probability <= 0:
        return node_types
    out = []
    for nt in node_types:
        r = stream(seed, "clusters", nt.name)
        if r.random() < cfg.clusters_probability:
            nt = dataclasses.replace(nt, clusters=r.choice((2, 3)))
        out.append(nt)
    return tuple(out)


def draw_write_form(rng: random.Random) -> WriteForm:
    """How a setup writes: the insert form, the NodeUpdate form, the transaction door,
    a cancelled transaction of junk rows, the EdgeUpdate form, and a delta merge of
    every column table after the data (`rebuild_indexes`: `MERGE DELTA OF`, which
    moves the rows from the write-optimized delta store into the main store)."""
    return WriteForm(
        insert_via=rng.choice(INSERT_FORMS),
        update_form=rng.choice(UPDATE_FORMS),
        transaction=rng.choice(TRANSACTION_FORMS),
        cancelled_junk=rng.random() < 0.5,
        edge_update_form=rng.choice(EDGE_UPDATE_FORMS),
        rebuild_indexes=rng.random() < 0.5,
    )
