"""The SAP HANA provider: this package's modules behind the core's contract.

Four data models of one SAP HANA Cloud database -- the JSON Document Store (`doc`),
column tables (`rel`), the Property Graph Engine's graph workspace over column tables
(`graph`) and the Knowledge Graph Engine's triple store (`rdf`) -- each asked every
question through its own query language: the document store's collection queries, SQL,
openCypher (inside SQL through `OPENCYPHER_TABLE`), and SPARQL (directly, and inside SQL
through `SPARQL_TABLE`). The comparison order puts `graph` next to `rel`: the same
bytes read through two engines."""
from __future__ import annotations

from ganda.core.provider import Provider
from ganda.hana import pivots
from ganda.hana.config import HanaConfig, refuse_unsupported
from ganda.hana.limits import LIMITS
from ganda.hana.semantics import SEMANTICS


class HanaProvider(Provider):
    name = "hana"
    title = "SAP HANA"
    data_models = ("doc", "rel", "graph", "rdf")
    config_class = HanaConfig
    limits = LIMITS
    semantics = SEMANTICS
    pivots = pivots
    coverage_levels = ("off",)

    @property
    def physical_design(self):
        from ganda.hana import physical_design

        return physical_design

    @property
    def mappers(self) -> dict:
        from ganda.hana.mapping import MAPPERS

        return MAPPERS

    @property
    def traversal_syntax(self):
        from ganda.hana.concrete_syntax import traversal

        return traversal

    @property
    def select_syntax(self):
        from ganda.hana.concrete_syntax import select

        return select

    @property
    def path_syntax(self):
        from ganda.hana.concrete_syntax import path

        return path

    @property
    def write_syntax(self):
        from ganda.hana.concrete_syntax import write

        return write

    @property
    def rewrites(self):
        from ganda.hana import rewrites

        return rewrites

    @property
    def known_bugs(self):
        from ganda.hana import known_bugs

        return known_bugs

    @property
    def mutants(self) -> list:
        from ganda.hana.mutants import MUTANTS

        return MUTANTS

    # --- the server -----------------------------------------------------------------------

    def server(self, args=None):
        from ganda.core import scale as _scale
        from ganda.hana.connection import HanaServer

        profile = _scale.profile(getattr(args, "scale", None) or "default")
        return HanaServer(timeout_ms=getattr(args, "query_timeout_ms", None) or profile.query_timeout_ms)

    def database_name(self, namespace: str, data_model: str) -> str:
        from ganda.hana.connection import db_name

        return db_name(namespace, data_model)

    def may_write(self, case) -> bool:
        """A transaction-door script writes, then rolls back."""
        from ganda.hana.concrete_syntax.tx import is_script

        return any(is_script(text) for text in case.queries.values())

    # --- the campaign ---------------------------------------------------------------------

    def configure(self, args, cfg):
        refuse_unsupported(cfg)
        return cfg

    def probe(self, facts: "list | None" = None) -> int:
        from ganda.hana.scripts import probe

        return probe.main(facts or [])


PROVIDER = HanaProvider()
