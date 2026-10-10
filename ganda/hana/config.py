"""The SAP HANA campaign configuration: the core's axes plus this engine's own, and the
core axes this provider does not model yet (refused rather than silently answered
wrong)."""
from __future__ import annotations

from dataclasses import dataclass

from ganda.core.data_generator import GeneratorConfig


@dataclass
class HanaConfig(GeneratorConfig):
    # the index on `id`, per data model: one of `dialect.ID_INDEX_KINDS` instead of the
    # PRIMARY KEY (rel) / no index (doc)
    id_index_probability: float = 0.5
    # a class's column table HASH-partitioned on `id` into 2 or 3 partitions (rel, graph)
    clusters_probability: float = 0.0
    # the graph workspace created NO CACHE instead of with the default dynamic cache
    # (graph): the openCypher queries read the tables without the engine's adjacency index
    graph_cache_probability: float = 0.0


# core axes whose types or semantics the provider does not model yet: the field and
# why (what has to be probed before the axis can be switched on)
UNSUPPORTED_AXES = {
    "narrow_type_probability": "INTEGER / REAL / DECIMAL columns: the conversions of a REAL against a double and of a DECIMAL in the doc and rdf data models are not modelled",
    "date_map_probability": "TIMESTAMP / DATE and map columns: no date literal or map form is modelled across the three data models",
    "collate_probability": "case-insensitive collation: HANA has no per-column ci collation to declare",
    "binary_probability": "VARBINARY columns: a byte string has no JSON or RDF form the three data models share",
    "inheritance_probability": "the abstract base class: neither a collection nor a column table inherits",
    "custom_key_probability": "custom keys: an astral code point in a key orders differently in SQL (UTF-16 code units) and SPARQL (code points), which the id tiebreak of an ordering meets",
}


def refuse_unsupported(cfg) -> None:
    on = [f"--{name.replace('_', '-')} ({why})" for name, why in UNSUPPORTED_AXES.items() if getattr(cfg, name, 0)]
    if on:
        raise SystemExit("the SAP HANA provider does not model these axes yet: " + "; ".join(on))
