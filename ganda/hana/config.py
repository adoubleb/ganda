"""The SAP HANA campaign configuration: the core's axes plus this engine's own, and the
core axes this provider does not model yet (refused rather than silently answered
wrong)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ganda.core.data.generator import GeneratorConfig


@dataclass
class HanaConfig(GeneratorConfig):
    HELP: ClassVar[dict] = {
        "id_index_probability": "chance the id index is a non-default kind instead of the PRIMARY KEY (rel) or no index (doc)",
        "clusters_probability": "chance a column table is hash-partitioned on id into 2 or 3 partitions (rel, graph)",
        "graph_cache_probability": "chance the graph workspace is created NO CACHE (graph data model)",
    }

    # HANA must have at least 2 node classes or else, bug H7 will trigger
    min_node_types: int = 2
    # the index on `id`, per data model: one of `dialect.ID_INDEX_KINDS` instead of the
    # PRIMARY KEY (rel) / no index (doc)
    id_index_probability: float = 0.5
    # a class's column table HASH-partitioned on `id` into 2 or 3 partitions (rel, graph)
    clusters_probability: float = 0.0
    # the graph workspace created NO CACHE instead of with the default dynamic cache
    # (graph): the openCypher queries read the tables without the engine's adjacency index
    graph_cache_probability: float = 0.0


# core axes whose types or semantics the provider does not model yet: the field and
# why (what has to be probed before the axis can be switched on). The custom-key axis
# is modelled (probe R18 / D22 / K21 / G28): a key travels as a bound NVARCHAR(256)
# value, a JSON string and a percent-encoded IRI, and the key pool holds no code point
# in U+E000..U+FFFF, the one range where SQL's UTF-16 order and SPARQL's code-point
# order of an astral key would part (`tests/hana/test_units.py` pins the pool). The
# inheritance axis is modelled too (`mapping/relational.py`: the base class as a UNION
# ALL view; `mapping/rdf.py`: a second class triple; the graph data model's unlabelled
# MATCH; the doc views' union).
UNSUPPORTED_AXES = {
    "narrow_type_probability": "INTEGER / REAL / DECIMAL columns: the conversions of a REAL against a double and of a DECIMAL in the doc and rdf data models are not modelled",
    "date_map_probability": "TIMESTAMP / DATE and map columns: no date literal or map form is modelled across the four data models",
    "collate_probability": "case-insensitive collation: HANA has no per-column ci collation to declare",
    "binary_probability": "VARBINARY columns: a byte string has no JSON or RDF form the four data models share",
}


def refuse_unsupported(cfg) -> None:
    on = [f"--{name.replace('_', '-')} ({why})" for name, why in UNSUPPORTED_AXES.items() if getattr(cfg, name, 0)]
    if on:
        raise SystemExit("the SAP HANA provider does not model these axes yet: " + "; ".join(on))
