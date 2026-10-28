"""The ArangoDB provider: this package's modules behind the core's contract.

Three data models of one ArangoDB server -- embedded link arrays (`doc`), edge
collections (`graph`) and junction collections (`rel`) -- each asked every question in
AQL through its own storage path. `rules/catalog.py` holds the engine rules the oracle
rests on (`ganda arangodb rules` renders them), `rules/baseline.json` the build the probe
last verified."""
from __future__ import annotations

from ganda.core.engine.provider import Provider
from ganda.arangodb.oracle import predicate_sampling
from ganda.arangodb.config import ArangoConfig
from ganda.arangodb.engine.limits import LIMITS
from ganda.arangodb.oracle.semantics import SEMANTICS


class ArangoDBProvider(Provider):
    name = "arangodb"
    title = "ArangoDB"
    data_models = ("doc", "graph", "rel")
    config_class = ArangoConfig
    limits = LIMITS
    semantics = SEMANTICS
    predicate_sampling = predicate_sampling
    coverage_levels = ("off", "seed", "case", "variant")

    # The modules below import this one's siblings; they are looked up on first use.

    @property
    def physical_design(self):
        from ganda.arangodb.storage import physical_design

        return physical_design

    @property
    def mappers(self) -> dict:
        from ganda.arangodb.storage.mapping import MAPPERS

        return MAPPERS

    @property
    def traversal_syntax(self):
        from ganda.arangodb.concrete_syntax import traversal

        return traversal

    @property
    def select_syntax(self):
        from ganda.arangodb.concrete_syntax import select

        return select

    @property
    def path_syntax(self):
        from ganda.arangodb.concrete_syntax import path

        return path

    @property
    def write_syntax(self):
        from ganda.arangodb.concrete_syntax import write

        return write

    @property
    def known_bugs(self):
        from ganda.arangodb.known_bugs import guards as known_bugs

        return known_bugs

    @property
    def rules(self):
        from ganda.arangodb.rules import CATALOG

        return CATALOG

    @property
    def baseline(self):
        from ganda.arangodb.rules import BASELINE

        return BASELINE

    @property
    def mutants(self) -> list:
        from ganda.arangodb.mutation.mutants import MUTANTS

        return MUTANTS

    # --- the server -----------------------------------------------------------------------

    def server(self, args=None):
        """A server for the campaign's scale: the profile's query timeout as every
        session's `maxRuntime`."""
        from ganda.core.data import scale as _scale
        from ganda.arangodb.engine.connection import ArangoServer

        profile = _scale.profile(getattr(args, "scale", None) or "default")
        return ArangoServer(query_timeout_ms=getattr(args, "query_timeout_ms", None) or profile.query_timeout_ms, profile_dir=_profile_dir(args))

    def coverage(self, server, args):
        """LLVM source-based coverage of an instrumented build (`ganda.core.engine.llvm_coverage`;
        the image is `docs/coverage.md`'s) under `--coverage`."""
        from ganda.core.engine.llvm_coverage import collector, module_of

        # the build instruments its bundled libraries (V8, ICU, RocksDB, ...) too: only
        # ArangoDB's own namespace is measured
        return collector(server, args, scope=lambda name: module_of(name, 1) == "arangodb")

    def database_name(self, namespace: str, data_model: str) -> str:
        from ganda.arangodb.engine.connection import db_name

        return db_name(namespace, data_model)

    def cleanup(self) -> None:
        from ganda.arangodb.engine.connection import cleanup_orphans

        cleanup_orphans()

    def may_write(self, case) -> bool:
        """A transaction door script writes, then aborts."""
        from ganda.arangodb.engine.tx import is_script

        return any(is_script(text) for text in case.queries.values())

    # --- the campaign ---------------------------------------------------------------------

    def probe(self, facts: "list | None" = None) -> int:
        from ganda.arangodb.scripts import probe

        return probe.main(facts or [])


PROVIDER = ArangoDBProvider()


def _profile_dir(args) -> "str | None":
    from ganda.core.engine.llvm_coverage import profile_dir

    return profile_dir(args) if args is not None else None
