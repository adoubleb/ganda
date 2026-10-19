"""The SurrealDB provider -- a SKELETON: the connection layer and the contract's
identity, no query supply yet. `ganda surrealdb doctor` exercises what is here;
`PORTING.md` says what each remaining slot takes from the legacy port."""
from __future__ import annotations

from ganda.core.data.generator import GeneratorConfig
from ganda.core.data.limits import StorageLimits
from ganda.core.engine.provider import Provider


class SurrealDBProvider(Provider):
    name = "surrealdb"
    title = "SurrealDB"
    data_models = ("doc", "graph", "rel")
    config_class = GeneratorConfig
    # the conservative bounds until this engine's are probed (`ganda surrealdb limits`)
    limits = StorageLimits.conservative()
    has_generators = False


    def server(self, args=None):
        from ganda.surrealdb.connection import SurrealServer

        return SurrealServer()

    def cleanup(self) -> None:
        from ganda.surrealdb.connection import cleanup_orphans

        cleanup_orphans()


PROVIDER = SurrealDBProvider()
