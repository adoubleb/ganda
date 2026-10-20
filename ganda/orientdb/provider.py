"""The OrientDB provider: this package's modules behind the core's contract."""
from __future__ import annotations

from ganda.core.engine.provider import Provider
from ganda.orientdb.oracle import pivots
from ganda.orientdb.config import OrientConfig
from ganda.orientdb.engine.limits import LIMITS
from ganda.orientdb.oracle.semantics import SEMANTICS


class OrientDBProvider(Provider):
    name = "orientdb"
    title = "OrientDB"
    data_models = ("doc", "graph", "rel")
    config_class = OrientConfig
    limits = LIMITS
    semantics = SEMANTICS
    pivots = pivots

    # The modules below import this one's siblings; they are looked up on first use.

    @property
    def physical_design(self):
        from ganda.orientdb.storage import physical_design

        return physical_design

    @property
    def mappers(self) -> dict:
        from ganda.orientdb.storage.mapping import MAPPERS

        return MAPPERS

    @property
    def traversal_syntax(self):
        from ganda.orientdb.concrete_syntax import traversal as traversal_syntax

        return traversal_syntax

    @property
    def select_syntax(self):
        from ganda.orientdb.concrete_syntax import select as select_syntax

        return select_syntax

    @property
    def path_syntax(self):
        from ganda.orientdb.concrete_syntax import path as path_syntax

        return path_syntax

    @property
    def write_syntax(self):
        from ganda.orientdb.concrete_syntax import write as write_syntax

        return write_syntax

    @property
    def rewrites(self):
        from ganda.orientdb.oracle import rewrites

        return rewrites

    @property
    def known_bugs(self):
        from ganda.orientdb.known_bugs import guards as known_bugs

        return known_bugs

    @property
    def mutants(self) -> list:
        from ganda.orientdb.mutation.mutants import MUTANTS

        return MUTANTS

    # --- the server -----------------------------------------------------------------------

    def server(self, args=None):
        """A server for the campaign's scale: every server takes request bodies up to
        the declared bound; the large scale gets a bigger heap and the profile's
        query timeout."""
        from ganda.core.data import scale as _scale
        from ganda.orientdb.engine.connection import LARGE_SCALE_MEMORY, OrientServer

        profile = _scale.profile(getattr(args, "scale", None) or "default")
        return OrientServer(
            coverage=getattr(args, "coverage", "off") != "off",
            query_timeout_ms=getattr(args, "query_timeout_ms", None) or profile.query_timeout_ms,
            http_max_length=LIMITS.max_request_bytes,
            memory=LARGE_SCALE_MEMORY if profile.name == "large" else None,
        )


    def database_name(self, namespace: str, data_model: str) -> str:
        from ganda.orientdb.engine.connection import db_name

        return db_name(namespace, data_model)

    def cleanup(self) -> None:
        from ganda.orientdb.engine.connection import cleanup_orphans

        cleanup_orphans()

    def may_write(self, case) -> bool:
        """A script text is the transaction door: it writes, then rolls back."""
        from ganda.orientdb.engine.connection import is_script

        return any(is_script(text) for text in case.queries.values())

    # --- the campaign ---------------------------------------------------------------------

    def add_arguments(self, parser) -> None:
        parser.add_argument("--keep-dictionary-duplicates", action="store_true", help="keep DICTIONARY indexes over duplicate values (O4) instead of demoting them")

    def configure(self, args, cfg):
        from ganda.orientdb.known_bugs import guards as known_bugs

        keep = bool(getattr(args, "keep_dictionary_duplicates", False))
        known_bugs.DICTIONARY_DUPLICATES = keep
        return cfg.with_(demote_colliding_indexes=False) if keep else cfg

    def coverage(self, server, args):
        """The JaCoCo collector (`ganda.orientdb.engine.coverage`) under `--coverage`."""
        level = getattr(args, "coverage", "off")
        if level == "off":
            return None
        from ganda.orientdb.engine.coverage import Collector

        return Collector(server, level)

    def probe(self, facts: "list | None" = None) -> int:
        """Re-check live every engine fact the provider rests on."""
        from ganda.orientdb.scripts import probe_osql

        return probe_osql.main()


PROVIDER = OrientDBProvider()
