"""The ArangoDB provider: doc (link arrays), graph (edge collections) and rel (junction
collections), every question in AQL."""
from __future__ import annotations

from ganda.core.engine.provider import Provider, Semantics
from ganda.arangodb.provider.config import ArangoConfig
from ganda.arangodb.engine import semantics
from ganda.arangodb.engine.limits import LIMITS
from ganda.arangodb.concrete_syntax.query import predicate_sampling
from ganda.arangodb.concrete_syntax.query.predicate_sampling import predicate_steering


class ArangoSemantics(Semantics):
    """ArangoDB's value semantics as the core's `Semantics`: the value order, equality
    and arithmetic of `engine.semantics`, and how a scan answers a predicate
    (`predicate_steering`)."""

    key = staticmethod(semantics.aql_key)
    eq = staticmethod(semantics.eq)
    lt = staticmethod(semantics.lt)
    predicate_hit = staticmethod(predicate_steering.predicate_hit)
    evaluate = staticmethod(predicate_steering.evaluate)
    apply_arith = staticmethod(semantics.apply_arith)
    arith_exact = staticmethod(semantics.arith_exact)
    sum_is_exact = staticmethod(semantics.sum_is_exact)

    def canonical(self, value):
        """The wire form: an integral double comes back as an integer (`2.0` is `2`)."""
        return semantics.canonical(value)


SEMANTICS = ArangoSemantics()


class ArangoDBProvider(Provider):
    name = "arangodb"
    title = "ArangoDB"
    data_models = ("doc", "graph", "rel")
    config_class = ArangoConfig
    limits = LIMITS
    semantics = SEMANTICS
    predicate_sampling = predicate_sampling
    coverage_levels = ("off",)

    # The modules below import this one's siblings; they are looked up on first use.

    @property
    def physical_design(self):
        from ganda.arangodb.provider import physical_design

        return physical_design

    @property
    def mappers(self) -> dict:
        from ganda.arangodb.concrete_syntax.setup import MAPPERS

        return MAPPERS

    @property
    def traversal_syntax(self):
        from ganda.arangodb.concrete_syntax.query import traversal as traversal_syntax

        return traversal_syntax

    @property
    def select_syntax(self):
        from ganda.arangodb.concrete_syntax.query import select as select_syntax

        return select_syntax

    @property
    def path_syntax(self):
        from ganda.arangodb.concrete_syntax.query import path as path_syntax

        return path_syntax

    @property
    def write_syntax(self):
        from ganda.arangodb.concrete_syntax.query import write as write_syntax

        return write_syntax

    @property
    def known_bugs(self):
        from ganda.arangodb import known_bugs

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
        return ArangoServer(query_timeout_ms=getattr(args, "query_timeout_ms", None) or profile.query_timeout_ms)

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
        from ganda.arangodb.rules import probe

        return probe.main(facts or [])


PROVIDER = ArangoDBProvider()
