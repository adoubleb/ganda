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

    # the modules below import this one's siblings; they are looked up on first use
    @property
    def traversal_syntax(self):
        from ganda.orientdb.queries import traversal_syntax

        return traversal_syntax

    @property
    def select_syntax(self):
        from ganda.orientdb.queries import select_syntax

        return select_syntax

    @property
    def path_syntax(self):
        from ganda.orientdb.queries import path_syntax

        return path_syntax

    @property
    def write_syntax(self):
        from ganda.orientdb.queries import write_syntax

        return write_syntax

    @property
    def known_bugs(self):
        from ganda.orientdb import known_bugs

        return known_bugs

    @property
    def rewrites(self):
        from ganda.orientdb import rewrites

        return rewrites

    @property
    def physical_design(self):
        from ganda.orientdb import physical_design

        return physical_design

    def schema(self, *args, **kwargs) -> Schema:
        """A `Schema` bound to this engine (hand-built schemas in tests and probes)."""
        return Schema(*args, engine=self, **kwargs)


PROVIDER = OrientDBProvider()
