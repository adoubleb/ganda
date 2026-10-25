"""Instance generation bound to OrientDB, for the OrientDB tests."""
from __future__ import annotations

from ganda.core.data import generator
from ganda.orientdb.config import OrientConfig as GeneratorConfig  # noqa: F401
from ganda.orientdb.provider import PROVIDER


def build(seed: int, cfg):
    """(schema, instance, query rng) for a (seed, config) on OrientDB."""
    return generator.build(seed, cfg, PROVIDER)
