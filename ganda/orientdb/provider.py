"""The OrientDB provider: this package's modules behind the core's contract."""
from __future__ import annotations

from ganda.core.provider import Provider
from ganda.orientdb import pivots
from ganda.orientdb.config import OrientConfig
from ganda.orientdb.semantics import SEMANTICS


class OrientDBProvider(Provider):
    name = "orientdb"
    title = "OrientDB"
    data_models = ("doc", "graph", "rel")
    config_class = OrientConfig
    semantics = SEMANTICS
    pivots = pivots

    # The modules below import this one's siblings; they are looked up on first use.

    @property
    def physical_design(self):
        from ganda.orientdb import physical_design

        return physical_design

    @property
    def mappers(self) -> dict:
        from ganda.orientdb.mapping import MAPPERS

        return MAPPERS

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
    def rewrites(self):
        from ganda.orientdb import rewrites

        return rewrites

    @property
    def known_bugs(self):
        from ganda.orientdb import known_bugs

        return known_bugs

    @property
    def mutants(self) -> list:
        from ganda.orientdb.mutants import MUTANTS

        return MUTANTS

    # --- the server -----------------------------------------------------------------------

    def server(self, args=None):
        from ganda.orientdb.connection import OrientServer

        return OrientServer(coverage=getattr(args, "coverage", "off") != "off")

    def database_name(self, namespace: str, data_model: str) -> str:
        from ganda.orientdb.connection import db_name

        return db_name(namespace, data_model)

    def cleanup(self) -> None:
        from ganda.orientdb.connection import cleanup_orphans

        cleanup_orphans()

    def may_write(self, case) -> bool:
        """A script text is the transaction door: it writes, then rolls back."""
        from ganda.orientdb.connection import is_script

        return any(is_script(text) for text in case.queries.values())

    # --- the campaign ---------------------------------------------------------------------

    def add_arguments(self, parser) -> None:
        parser.add_argument("--keep-dictionary-duplicates", action="store_true", help="keep DICTIONARY indexes over duplicate values (O4) instead of demoting them")

    def configure(self, args, cfg):
        from ganda.orientdb import known_bugs

        keep = bool(getattr(args, "keep_dictionary_duplicates", False))
        known_bugs.DICTIONARY_DUPLICATES = keep
        return cfg.with_(demote_colliding_indexes=False) if keep else cfg

    def coverage(self, server, args):
        """The JaCoCo collector (`ganda.orientdb.coverage`) under `--coverage`."""
        level = getattr(args, "coverage", "off")
        if level == "off":
            return None
        from ganda.orientdb.coverage import Collector

        return Collector(server, level)

    def probe(self) -> int:
        """Re-check live every engine fact the provider rests on."""
        from ganda.orientdb.scripts import probe_osql

        return probe_osql.main()


PROVIDER = OrientDBProvider()
