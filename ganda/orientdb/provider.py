"""The OrientDB provider: this package's modules behind the core's contract."""
from __future__ import annotations

import functools

from ganda.core.model import Schema
from ganda.orientdb import pivots
from ganda.orientdb.semantics import SEMANTICS


class OrientDBProvider:
    name = "orientdb"
    data_models = ("doc", "graph", "rel")
    semantics = SEMANTICS
    pivots = pivots

    def schema(self, *args, **kwargs) -> Schema:
        """A `Schema` bound to this engine (hand-built schemas in tests and probes)."""
        return Schema(*args, engine=self, **kwargs)


PROVIDER = OrientDBProvider()
