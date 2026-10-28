"""The OrientDB provider: this package's modules behind the core's contract."""
from __future__ import annotations

from ganda.core.engine.provider import Provider, Semantics
from ganda.orientdb.provider.config import OrientConfig
from ganda.orientdb.engine import semantics
from ganda.orientdb.engine.limits import LIMITS
from ganda.orientdb.concrete_syntax.query import predicate_sampling
from ganda.orientdb.concrete_syntax.query.predicate_sampling import predicate_steering


class OrientSemantics(Semantics):
    """OrientDB's value semantics as the core's `Semantics`: the value order, equality
    and arithmetic of `engine.semantics`, and how a scan answers a predicate
    (`predicate_steering`)."""

    key = staticmethod(semantics.orient_key)
    eq = staticmethod(semantics.eq)
    lt = staticmethod(semantics.lt)
    predicate_hit = staticmethod(predicate_steering.predicate_hit)
    evaluate = staticmethod(predicate_steering.evaluate)
    apply_arith = staticmethod(semantics.apply_arith)
    arith_exact = staticmethod(semantics.arith_exact)
    sum_is_exact = staticmethod(semantics.sum_is_exact)


SEMANTICS = OrientSemantics()


class OrientDBProvider(Provider):
    name = "orientdb"
    title = "OrientDB"
    data_models = ("doc", "graph", "rel")
    config_class = OrientConfig
    limits = LIMITS
    semantics = SEMANTICS
    predicate_sampling = predicate_sampling

    # The modules below import this one's siblings; they are looked up on first use.

    @property
    def physical_design(self):
        from ganda.orientdb.provider import physical_design

        return physical_design

    @property
    def mappers(self) -> dict:
        from ganda.orientdb.concrete_syntax.setup import MAPPERS

        return MAPPERS

    @property
    def traversal_syntax(self):
        from ganda.orientdb.concrete_syntax.query import traversal as traversal_syntax

        return traversal_syntax

    @property
    def select_syntax(self):
        from ganda.orientdb.concrete_syntax.query import select as select_syntax

        return select_syntax

    @property
    def path_syntax(self):
        from ganda.orientdb.concrete_syntax.query import path as path_syntax

        return path_syntax

    @property
    def write_syntax(self):
        from ganda.orientdb.concrete_syntax.query import write as write_syntax

        return write_syntax

    @property
    def known_bugs(self):
        from ganda.orientdb import known_bugs

        return known_bugs

    @property
    def rules(self):
        from ganda.orientdb.rules import CATALOG

        return CATALOG

    @property
    def baseline(self):
        from ganda.orientdb.rules import BASELINE

        return BASELINE

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

    def coverage(self, server, args):
        """The JaCoCo collector (`ganda.orientdb.code_coverage.collector`) under `--coverage`."""
        level = getattr(args, "coverage", "off")
        if level == "off":
            return None
        from ganda.core.engine.coverage import window_log_path
        from ganda.orientdb.code_coverage.collector import Collector

        return Collector(server, level, log_path=window_log_path(args))

    def probe(self, facts: "list | None" = None) -> int:
        """Re-check live every engine fact the provider rests on."""
        from ganda.orientdb.rules import probe

        return probe.main()


PROVIDER = OrientDBProvider()
