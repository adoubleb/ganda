"""Schema and instance generation bound to OrientDB: the core's data generator with
this provider and its configuration filled in."""
from __future__ import annotations

from ganda.core.data import generator as _core
from ganda.core.data.generator import initial_node_values, write_order_states  # noqa: F401
from ganda.orientdb.config import OrientConfig as GeneratorConfig  # noqa: F401
from ganda.orientdb.provider import PROVIDER


def build(seed: int, cfg):
    """(schema, instance, query rng) for a (seed, config) on OrientDB."""
    return _core.build(seed, cfg, PROVIDER)
