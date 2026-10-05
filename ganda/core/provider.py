"""The contract between the engine-agnostic core and one DBMS.

A `Provider` is everything the core needs to know about an engine. The core never
imports a provider package: it reaches the engine through the provider object a schema
was generated for (`Schema.engine`) or one passed to it explicitly.
"""
from __future__ import annotations

from typing import Any


class EngineError(RuntimeError):
    """The engine rejected a statement. `internal` marks an answer that is a defect in
    itself (a raw exception from inside the engine), whatever the other variants did."""

    def __init__(self, message: str, internal: bool = False, status: int = 0):
        super().__init__(message)
        self.internal = internal
        self.status = status


class EngineCrash(RuntimeError):
    """The server stopped answering (a dead process, a transport failure)."""


class Semantics:
    """The engine's value semantics: what the reference interpreter must reproduce to
    predict an answer from the `Instance`."""

    def key(self, v: Any):
        """A total-order key reproducing the engine's comparison inside one type, and a
        stable rank across types. The default is structural: null < bool < number <
        string (code points) < list < map, `-0.0` below `0.0`."""
        if v is None:
            return (0,)
        if isinstance(v, bool):
            return (1, int(v))
        if isinstance(v, (int, float)):
            if v == 0:
                return (2, 0, -1 if str(v).startswith("-") else 0)
            return (2, v, 0)
        if isinstance(v, str):
            return (3, v)
        if isinstance(v, list):
            return (4, tuple(self.key(x) for x in v))
        if isinstance(v, dict):
            return (5, tuple(sorted((k, self.key(x)) for k, x in v.items())))
        return (6, repr(v))

    def eq(self, a: Any, b: Any) -> bool:
        raise NotImplementedError

    def lt(self, a: Any, b: Any) -> bool:
        raise NotImplementedError

    def predicate_hit(self, present: bool, v: Any, op: str, pivot: Any, ftype: "str | None" = None, ci: bool = False, key: "str | None" = None) -> bool:
        """Whether a stored value (absent when not `present`) satisfies `op pivot`."""
        raise NotImplementedError

    def evaluate(self, values: dict, p) -> bool:
        """Whether a record satisfies a predicate tree (the engine's boolean logic)."""
        raise NotImplementedError

    def apply_arith(self, v: Any, sym: str, c: Any, ftype: "str | None"):
        """`v <sym> c` as the engine computes it."""
        raise NotImplementedError

    def agg(self, fn: str, values: list, ftype: "str | None" = None):
        raise NotImplementedError

    def mean(self, values: list):
        raise NotImplementedError

    def sum_is_exact(self, values: list, ftype: "str | None" = "int") -> bool:
        """Whether the engine's sum of `values` is exact in every order."""
        raise NotImplementedError

    def aggregate_over_no_rows(self, fn: str) -> list:
        """The rows an aggregate other than a count answers over no input rows."""
        raise NotImplementedError

    def nulls_first(self, desc: bool) -> bool:
        """Whether an ORDER BY puts the null keys first in this direction."""
        raise NotImplementedError

    def order_key(self, v: Any, desc: bool):
        """A per-key sort key for an ORDER BY: `key` with the engine's null placement,
        reversed for a descending key."""
        raise NotImplementedError

    def orders_exactly(self, ftype, custom_keys: bool) -> bool:
        """Whether the reference reproduces an id-tiebroken ORDER BY on a column of
        `ftype` (a collation the reference does not model makes it inexact)."""
        raise NotImplementedError


class Session:
    """One database of a running server: the data model it holds."""

    def apply_setup(self, plan: list) -> None:
        """Run a setup plan (the provider's own steps, `ganda.core.setup_plan`)."""
        raise NotImplementedError

    def execute(self, text: str, binds: "dict | None" = None, options: "dict | None" = None) -> list:
        """One query text of a case with the case's bind variables: its rows, the
        engine's record metadata stripped. Raises `EngineError` for a rejection and
        `EngineCrash` when the server is gone."""
        raise NotImplementedError

    def explain(self, text: str, binds: "dict | None" = None) -> "list | None":
        """A fingerprint of the plan the engine would run, or None when it has none."""
        return None

    def read_nodes(self, table: str) -> dict:
        """{logical id: stored field values} of a node class (`state_check`)."""
        raise NotImplementedError

    def read_edges(self, data_model: str, instance, edge_type) -> list:
        """[(edge id, from id, to id, {attribute: value})] as the data model stores a
        relationship type (`state_check`)."""
        raise NotImplementedError


class Server:
    """A running engine: a context manager that starts it (or attaches to one) and
    hands out sessions."""

    external = False  # attached to a server the campaign did not start (never restarted)

    def __enter__(self) -> "Server":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def fresh_session(self, database: str) -> Session:
        """A session on a database created empty (dropped first when it exists)."""
        raise NotImplementedError

    def drop_database(self, database: str) -> None:
        raise NotImplementedError

    def diagnostics(self) -> str:
        """What is known about a server that crashed (logs, exit state)."""
        return ""


class Provider:
    """One DBMS behind the core. The attributes are the SLOTS the core reads; a slot may
    be an object or a module, as long as it has the members named here.

    name            the engine's id on the command line and in bundles
    data_models     the storage models an instance is materialized in, in comparison order
    config_class    the campaign configuration (`GeneratorConfig` or a subclass adding
                    the engine's own axes); every field is a command-line flag
    semantics       `Semantics`: what the reference interpreter must reproduce
    pivots          predicate sampling: NULL_OPS, ORDERED_OPS, LIKE_WILDCARDS,
                    cross_type(rng, ftype, v), refine(rng, ftype, v)
    physical_design index kinds and what they enforce, write forms, key rules:
                    draw_index_kind(rng), NON_UNIQUE_KINDS, EDGE_INDEX_DATA_MODELS,
                    is_unique(ix), holds_one_record_per_key(ix), demoted_kind(kind),
                    index_key_value(v, ci), draw_id_indexes(seed, cfg),
                    draw_storage_options(seed, cfg, node_types), draw_write_form(rng),
                    is_valid_key(key), BOUNDARY_VALUES
    mappers         {data model: Instance -> setup plan}
    traversal_syntax  syntaxes_for(q, schema, dm), render(q, schema, dm, syntax,
                    pull_up=False), can_pull_up(q)
    select_syntax   BASE, METHODS, method_fn(template, ftype), build_rewrites(q, rng,
                    schema, instance), extra_binds(q, schema, rewrites),
                    rewrite_params(rewrites), base_data_models(q),
                    syntaxes_for(q, schema, rewrites, dm), applicable(q, schema, syntax,
                    rewrites, dm), render(q, schema, syntax, rewrites, dm)
    path_syntax     render_all(q, schema, instance) -> (queries, known bugs), bind_vars(q)
    write_syntax    build_rewrites(q, rng, schema, instance), all_syntaxes(q, dm),
                    applicable(q, syntax, rewrites, dm), render(q, syntax, rewrites, dm,
                    before, after) -> statements, execute(session, statements),
                    shared_binds(statements), rewrite_params(rewrites)
    rewrites        arith_exact(v, sym, c, ftype)
    known_bugs      mark_known_bugs(case, q, schema, instance) -> case,
                    select_known(q, syntax, rewrites, dm, schema, instance) -> id | None,
                    write_known(q, syntax, rewrites, dm, schema, instance) -> id | None
    mutants         the mutation benchmark's catalog (`ganda.core.mutation.mutants`)
    extra_generators  {generator name: (rng, schema, instance, cfg) -> cases}: the
                    engine-only query generators, with `reference_for` for their answers
    """

    name = ""
    title = ""
    data_models = ("doc", "graph", "rel")
    config_class = None
    semantics: Semantics = None
    pivots = None
    physical_design = None
    mappers: dict = {}
    traversal_syntax = None
    select_syntax = None
    path_syntax = None
    write_syntax = None
    rewrites = None
    known_bugs = None
    mutants: list = []
    extra_generators: dict = {}
    coverage_levels = ("off", "seed", "case", "variant")
    # False for a provider that only has its connection layer so far
    has_generators = True

    def schema(self, *args, **kwargs):
        """A `Schema` bound to this engine (hand-built schemas in tests and probes)."""
        from ganda.core.model import Schema

        return Schema(*args, engine=self, **kwargs)

    # --- the server -----------------------------------------------------------------------

    def server(self, args=None) -> Server:
        """A server for a campaign (`args`: the parsed command line, or None)."""
        raise NotImplementedError

    def database_name(self, namespace: str, data_model: str) -> str:
        return f"fz_{namespace}_{data_model}"

    def cleanup(self) -> None:
        """Remove whatever a campaign may have left behind (orphaned containers)."""

    def may_write(self, case) -> bool:
        """Whether a READ case writes on the way (inside a transaction it rolls back):
        the campaign then re-verifies the stored state after the cases."""
        return False

    # --- the campaign ---------------------------------------------------------------------

    def add_arguments(self, parser) -> None:
        """The engine's own command-line flags."""

    def configure(self, args, cfg):
        """Apply the engine's flags; the configuration the campaign runs."""
        return cfg

    def coverage(self, server, args):
        """A `ganda.core.coverage.Collector` under `--coverage`, or None."""
        if getattr(args, "coverage", "off") != "off":
            raise SystemExit(f"{self.name} has no code-coverage collector")
        return None

    def reference_for(self, case, instance):
        """(has a reference, the expected answer) for a case of an extra generator."""
        return False, None

    def probe(self) -> int:
        """Re-check live the engine facts the provider rests on; a process exit code."""
        print(f"{self.name} has no probe")
        return 0


# the value order of a case no provider wrote (hand-built cases)
DEFAULT_SEMANTICS = Semantics()
