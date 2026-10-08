"""The ArangoDB provider -- a SKELETON: the connection layer and the contract's
identity, no query supply yet. `ganda arangodb doctor` exercises what is here;
`PORTING.md` says what each remaining slot takes from the legacy port."""
from __future__ import annotations

from ganda.core.data_generator import GeneratorConfig
from ganda.core.limits import StorageLimits
from ganda.core.provider import Provider


class ArangoDBProvider(Provider):
    name = "arangodb"
    title = "ArangoDB"
    data_models = ("doc", "graph", "rel")
    config_class = GeneratorConfig
    # the conservative bounds until this engine's are probed (`ganda arangodb limits`)
    limits = StorageLimits.conservative()
    has_generators = False


    def server(self, args=None):
        from ganda.arangodb.connection import ArangoServer

        return ArangoServer()

    def cleanup(self) -> None:
        from ganda.arangodb.connection import cleanup_orphans

        cleanup_orphans()


PROVIDER = ArangoDBProvider()
