"""Instance generation bound to SurrealDB, for the SurrealDB tests."""
from __future__ import annotations

from ganda.core.data import generator
from ganda.surrealdb.provider import PROVIDER
from ganda.surrealdb.provider.config import SurrealConfig as GeneratorConfig  # noqa: F401

# every axis the core and this provider draw, on
EVERY_AXIS = dict(
    write_form_probability=1.0,
    custom_key_probability=0.3,
    narrow_type_probability=0.5,
    date_map_probability=0.5,
    list_index_probability=0.5,
    inheritance_probability=0.5,
    binary_probability=0.3,
    churn_update_probability=0.5,
    churn_delete_probability=0.3,
    churn_node_delete_probability=0.3,
    churn_edge_update_probability=0.3,
    schemafull_probability=0.5,
)


def build(seed: int, cfg):
    """(schema, instance, query rng) for a (seed, config) on SurrealDB."""
    return generator.build(seed, cfg, PROVIDER)
