"""The SurrealDB provider: this package's modules behind the core's contract."""
from __future__ import annotations

import re

from ganda.core.engine.provider import Provider, Semantics
from ganda.surrealdb.engine import semantics
from ganda.surrealdb.engine.limits import LIMITS
from ganda.surrealdb.provider.config import SurrealConfig


class SurrealSemantics(Semantics):
    """SurrealDB's value semantics as the core's `Semantics`: the value order, equality
    and arithmetic of `engine.semantics`, and how a scan answers a predicate
    (`predicate_steering`)."""

    key = staticmethod(semantics.surreal_key)
    eq = staticmethod(semantics.eq)
    lt = staticmethod(semantics.lt)
    apply_arith = staticmethod(semantics.apply_arith)
    arith_exact = staticmethod(semantics.arith_exact)
    sum_is_exact = staticmethod(semantics.sum_is_exact)
    canonical = staticmethod(semantics.canonical)

    def predicate_hit(self, present, v, op, constant, ftype=None, ci=False, key=None):
        from ganda.surrealdb.concrete_syntax.query.predicate_sampling import predicate_steering

        return predicate_steering.predicate_hit(present, v, op, constant, ftype, ci, key)

    def evaluate(self, values, p):
        from ganda.surrealdb.concrete_syntax.query.predicate_sampling import predicate_steering

        return predicate_steering.evaluate(values, p)


SEMANTICS = SurrealSemantics()
# the error's text with its quoted names, record ids and numbers taken out
_SPECIFICS = re.compile(r"`[^`]*`|'[^']*'|\"[^\"]*\"|\b[A-Za-z0-9_]+:[^\s,]+|\d+")


class SurrealDBProvider(Provider):
    name = "surrealdb"
    title = "SurrealDB"
    data_models = ("doc", "graph", "rel")
    config_class = SurrealConfig
    limits = LIMITS
    semantics = SEMANTICS
    coverage_levels = ("off", "seed", "case", "variant")
    # a hop the doc data model cannot ask (an incoming hop with multiplicity) stays a
    # graph-vs-rel case
    partial_traversals = True

    # The modules below import this one's siblings; they are looked up on first use.

    @property
    def predicate_sampling(self):
        from ganda.surrealdb.concrete_syntax.query import predicate_sampling

        return predicate_sampling

    @property
    def physical_design(self):
        from ganda.surrealdb.provider import physical_design

        return physical_design

    @property
    def mappers(self) -> dict:
        from ganda.surrealdb.concrete_syntax.setup import MAPPERS

        return MAPPERS

    @property
    def traversal_syntax(self):
        from ganda.surrealdb.concrete_syntax.query import traversal as traversal_syntax

        return traversal_syntax

    @property
    def select_syntax(self):
        from ganda.surrealdb.concrete_syntax.query import select as select_syntax

        return select_syntax

    @property
    def path_syntax(self):
        from ganda.surrealdb.concrete_syntax.query import path as path_syntax

        return path_syntax

    @property
    def write_syntax(self):
        from ganda.surrealdb.concrete_syntax.query import write as write_syntax

        return write_syntax

    @property
    def known_bugs(self):
        from ganda.surrealdb import known_bugs

        return known_bugs

    @property
    def rules(self):
        from ganda.surrealdb.rules import CATALOG

        return CATALOG

    @property
    def baseline(self):
        from ganda.surrealdb.rules import BASELINE

        return BASELINE

    @property
    def mutants(self) -> list:
        from ganda.surrealdb.mutation.mutants import MUTANTS

        return MUTANTS

    def error_signature(self, message: str) -> str:
        """The error's class: its text without the names and numbers of the instance."""
        return _SPECIFICS.sub("", message).split("\n")[0].strip()[:80]

    # --- the server -----------------------------------------------------------------------

    def server(self, args=None):
        from ganda.core.data import scale as _scale
        from ganda.surrealdb.engine.connection import SurrealServer

        profile = _scale.profile(getattr(args, "scale", None) or "default")
        return SurrealServer(query_timeout_ms=getattr(args, "query_timeout_ms", None) or profile.query_timeout_ms, profile_dir=_profile_dir(args))

    def coverage(self, server, args):
        """LLVM source-based coverage of an instrumented build (`ganda.core.engine.llvm_coverage`;
        the image is `docs/coverage.md`'s) under `--coverage`."""
        from ganda.core.engine.llvm_coverage import collector

        return collector(server, args)

    def database_name(self, namespace: str, data_model: str) -> str:
        from ganda.surrealdb.engine.connection import db_name

        return db_name(namespace, data_model)

    def cleanup(self) -> None:
        from ganda.surrealdb.engine.connection import cleanup_orphans

        cleanup_orphans()

    def may_write(self, case) -> bool:
        """A transaction text is the read door: it writes, reads, then cancels."""
        return any(text.lstrip().upper().startswith("BEGIN") for text in case.queries.values())

    # --- the campaign ---------------------------------------------------------------------

    def probe(self, facts: "list | None" = None) -> int:
        """Re-check live every engine fact the provider rests on."""
        from ganda.surrealdb.rules import probe

        return probe.main(facts)


PROVIDER = SurrealDBProvider()


def _profile_dir(args) -> "str | None":
    from ganda.core.engine.llvm_coverage import profile_dir

    return profile_dir(args) if args is not None else None
