"""The contract between the engine-agnostic core and one DBMS.

A `Provider` is everything the core needs to know about an engine. The core never
imports a provider package: it reaches the engine through the provider object a schema
was generated for (`Schema.engine`) or one passed to it explicitly.
"""
from __future__ import annotations

from typing import Any


class EngineError(RuntimeError):
    """The engine rejected a statement. `internal` marks an answer that is a defect in
    itself (a raw exception from inside the engine), whatever the other variants did.
    `timeout` marks a statement the engine gave up on (or the transport did): no
    answer at all, which the oracle leaves out of the comparison rather than reading
    as a rejection."""

    def __init__(self, message: str, internal: bool = False, status: int = 0, timeout: bool = False):
        super().__init__(message)
        self.internal = internal
        self.status = status
        self.timeout = timeout


class EngineCrash(RuntimeError):
    """The server stopped answering (a dead process, a transport failure)."""


class Semantics:
    """The engine's value semantics: the value order and equality the oracle and the
    state check compare answers under, and the predicate logic and arithmetic the
    generators walk the `Instance` with (to draw filters that keep records, writes
    that match some, sums that stay exact). No answer is predicted from it."""

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

    def predicate_hit(self, present: bool, v: Any, op: str, constant: Any, ftype: "str | None" = None, ci: bool = False, key: "str | None" = None) -> bool:
        """Whether a stored value (absent when not `present`) satisfies `op constant`."""
        raise NotImplementedError

    def evaluate(self, values: dict, p) -> bool:
        """Whether a record satisfies a predicate tree (the engine's boolean logic)."""
        raise NotImplementedError

    def arith_exact(self, v: Any, sym: str, c: Any, ftype) -> bool:
        """Whether `v <sym> c` is computed exactly in a column of `ftype` (a
        `FieldType`): the write generator draws `SET f = f <sym> c` only then."""
        raise NotImplementedError

    def apply_arith(self, v: Any, sym: str, c: Any, ftype: "str | None"):
        """`v <sym> c` as the engine computes it."""
        raise NotImplementedError

    def sum_is_exact(self, values: list, ftype: "str | None" = "int") -> bool:
        """Whether the engine's sum of `values` is exact in every order."""
        raise NotImplementedError

    def canonical(self, value):
        """A bare answer's values in one wire form, so two variants' rows compare in the
        same form (an engine that prints an integral double as an integer says so
        here); the identity by default."""
        return value


class Session:
    """One database of a running server: the data model it holds."""

    def apply_setup(self, plan: list) -> None:
        """Run a setup plan (the provider's own steps, `ganda.core.engine.setup_plan`)."""
        raise NotImplementedError

    def execute(self, text: str, binds: "dict | None" = None, options: "dict | None" = None) -> list:
        """One query text of a case with the case's bind variables: its rows, the
        engine's record metadata stripped. Raises `EngineError` for a rejection and
        `EngineCrash` when the server is gone."""
        raise NotImplementedError

    def explain(self, text: str, binds: "dict | None" = None) -> "list | None":
        """A fingerprint of the plan the engine would run, or None when it has none."""
        return None

    def read_nodes(self, table: str, ids: "list | None" = None) -> dict:
        """{logical id: stored field values} of a node class (`state_check`); with
        `ids`, those records only (a provider that cannot select by id may answer
        them all: the caller filters)."""
        raise NotImplementedError

    def count_nodes(self, table: str) -> int:
        """How many records the class holds (a sampled state check reads the count
        and a sample instead of every record)."""
        return len(self.read_nodes(table))

    def read_edges(self, data_model: str, instance, edge_type, sources: "list | None" = None) -> list:
        """[(edge id, from id, to id, {attribute: value})] as the data model stores a
        relationship type (`state_check`); with `sources`, the relationships leaving
        those records only (a provider may answer them all: the caller filters)."""
        raise NotImplementedError

    def count_edges(self, data_model: str, instance, edge_type) -> int:
        """How many relationships of the type the data model holds."""
        return len(self.read_edges(data_model, instance, edge_type))


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

    def version(self) -> "str | None":
        """The engine's build, which a campaign compares with the provider's baseline at
        startup (None: the server cannot say, and nothing is compared)."""
        return None


class Provider:
    """One DBMS behind the core. The attributes are the SLOTS the core reads; a slot may
    be an object or a module, as long as it has the members named here.

    name            the engine's id on the command line and in bundles
    data_models     the storage models an instance is materialized in, in comparison order
    config_class    the campaign configuration (`GeneratorConfig` or a subclass adding
                    the engine's own axes); every field is a command-line flag
    limits          `ganda.core.data.limits.StorageLimits`: what the engine can hold, which
                    the large-data axes stay inside (`ganda <engine> limits` prints it)
    semantics       `Semantics`: the value order, equality and predicate logic
    predicate_sampling
                    the operators, constants and forms of a predicate leaf:
                    NULL_OPS, ORDERED_OPS, LIKE_WILDCARDS, ordered_ops(ftype)
                    (the comparison operators a column of the type admits: a
                    subset of ORDERED_OPS), cross_type(rng, ftype, v),
                    refine(rng, ftype, v); optionally leaf_form(rng, leaf) (the
                    drawn leaf in a form of the engine's: reversed, under arithmetic, ...)
    physical_design index kinds and what they enforce, write forms, key rules:
                    draw_index_kind(rng), NON_UNIQUE_KINDS, EDGE_INDEX_DATA_MODELS,
                    is_unique(ix), holds_one_record_per_key(ix), demoted_kind(kind),
                    index_key_value(v, ci), draw_id_indexes(seed, cfg),
                    draw_index_options(rng, ftype) (an index's `options`),
                    draw_storage_options(seed, cfg, node_types) (a node type's
                    `options`), draw_write_form(rng), is_valid_key(key),
                    BOUNDARY_VALUES; an id index left undrawn is the engine's default
    mappers         {data model: Instance -> setup plan}
    traversal_syntax  syntaxes_for(q, schema, dm), render(q, schema, dm, syntax,
                    pull_up=False), can_pull_up(q)
    select_syntax   BASE, METHODS, build_rewrites(q, rng,
                    schema, instance), extra_binds(q, schema, rewrites),
                    rewrite_params(rewrites), base_data_models(q),
                    syntaxes_for(q, schema, rewrites, dm), applicable(q, schema, syntax,
                    rewrites, dm), render(q, schema, syntax, rewrites, dm)
    path_syntax     render_all(q, schema, instance) -> (queries, known bugs), bind_vars(q)
    write_syntax    build_rewrites(q, rng, schema, instance), all_syntaxes(q, dm),
                    applicable(q, syntax, rewrites, dm), render(q, syntax, rewrites, dm,
                    before, after) -> statements, execute(session, statements),
                    shared_binds(statements), rewrite_params(rewrites), spelling(syntax)
                    -> what every data model's write shares (optional)
    known_bugs      mark_known_bugs(case, q, schema, instance) -> case,
                    select_known(q, syntax, rewrites, dm, schema, instance) -> id | None,
                    write_known(q, syntax, rewrites, dm, schema, instance) -> id | None
    mutants         the mutation benchmark's catalog (`ganda.core.mutation.mutants`)
    rules           `ganda.core.rules.Catalog`: the engine rules as typed records (None:
                    the provider has no catalog yet); `ganda <engine> rules` renders it
    baseline        `ganda.core.rules.baseline.Baseline`: the build the probe last
                    verified; a campaign against a server of another build warns at startup
    extra_generators  {generator name: (rng, schema, instance, cfg) -> cases}: the
                    engine-only query generators
    compare_despite_errors  the oracle compares the variants that answered when another
                    errored (default False: an error ends the comparison)
    partial_traversals  a traversal with no syntax on some data model stays as a partial
                    case (default False: the question is dropped)
    error_signature(message) -> str  the class of an error's text the finding key adds to
                    the erroring variant ('' by default: the key names variants alone)
    """

    name = ""
    title = ""
    data_models = ("doc", "graph", "rel")
    config_class = None
    limits = None  # a `StorageLimits`
    semantics: Semantics = None

    predicate_sampling = None
    physical_design = None
    mappers: dict = {}
    traversal_syntax = None
    select_syntax = None
    path_syntax = None
    write_syntax = None
    known_bugs = None
    mutants: list = []
    rules = None  # a `ganda.core.rules.Catalog`
    baseline = None  # a `ganda.core.rules.baseline.Baseline`
    extra_generators: dict = {}
    coverage_levels = ("off", "seed", "case", "variant")
    # False for a provider that only has its connection layer so far
    has_generators = True
    # the oracle compares the variants that answered even when another one errored: the
    # error asymmetry then carries any mismatch among the rest, and the finding key names
    # it (`oracle.build_result`, `dedup`). Off, an error ends the comparison -- the legacy
    # fuzzers' behaviour
    compare_despite_errors = False
    # a traversal question some data model has no concrete syntax for is kept as a
    # PARTIAL case (the others compare among themselves, as a select does); off, the
    # question is dropped on every data model -- the legacy behaviour
    partial_traversals = False

    def error_signature(self, message: str) -> str:
        """A short class of an engine error's text (its code, say) that the finding key of
        an error asymmetry or an internal error adds to the erroring variant's name, so
        two different refusals on one shape are two findings; '' (the default) keeps the
        key by variants alone."""
        return ""

    def schema(self, *args, **kwargs):
        """A `Schema` bound to this engine (hand-built schemas in tests and probes)."""
        from ganda.core.data.model import Schema

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
        """A `ganda.core.engine.coverage.Collector` under `--coverage`, or None."""
        if getattr(args, "coverage", "off") != "off":
            raise SystemExit(f"{self.name} has no code-coverage collector")
        return None

    def probe(self, facts: "list | None" = None) -> int:
        """Re-check live the engine facts the provider rests on; a process exit code.
        `facts` narrows to some of them (an id or a prefix); all of them if None."""
        print(f"{self.name} has no probe")
        return 0


# the value order of a case no provider wrote (hand-built cases)
DEFAULT_SEMANTICS = Semantics()
