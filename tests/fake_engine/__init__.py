"""An in-memory engine behind the provider contract: the test double the core's tests
run on.

Every data model's "database" is that data model's own copy of the `Instance`; a query
text is the JSON of the IR and a session answers it with the core's evaluators over
its copy. So a healthy campaign agrees with itself by construction, and a corruption
of one data model's copy (a mutant, a faulty session in a test) is a real disagreement
the oracle has to catch. No engine-specific package is imported here: if the core
needed one, these tests could not run.
"""
from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass, field

from ganda.core.query.ir import path as path_ast
from ganda.core.query.ir import select as select_ast
from ganda.core.query.ir import traversal as traversal_ast
from ganda.core.query.ir import write as write_ast
from ganda.core.query.ir.predicates import Compound
from ganda.core.data.generator import BOUNDARY_VALUES, GeneratorConfig
from ganda.core.data.limits import StorageLimits
from ganda.core.data.model import WriteForm
from ganda.core.mutation.mutants import INSTANCE_MUTANTS
from ganda.core.engine.provider import EngineError, Provider, Semantics, Server, Session
from ganda.core.engine.setup_plan import step

DATA_MODELS = ("doc", "graph", "rel")


# --- semantics: plain Python values, two-valued logic --------------------------------------


class FakeSemantics(Semantics):
    def eq(self, a, b) -> bool:
        return a is not None and b is not None and self.key(a) == self.key(b)

    def lt(self, a, b) -> bool:
        return self.key(a) < self.key(b)

    def predicate_hit(self, present, v, op, pivot, ftype=None, ci=False, key=None) -> bool:
        if key is not None:
            present = isinstance(v, dict) and key in v
            v = v.get(key) if isinstance(v, dict) else None
        null = not present or v is None
        if op == "IS NULL":
            return null
        if op == "IS NOT NULL":
            return not null
        if op == "IS DEFINED":
            return present
        if op == "IS NOT DEFINED":
            return not present
        if op == "CONTAINSKEY":
            return isinstance(v, dict) and pivot in v
        if op in ("VALUES CONTAINS", "CONTAINSVALUE"):
            return isinstance(v, dict) and any(self.eq(x, pivot) for x in v.values())
        if null or pivot is None:
            return False
        if op == "=":
            return self.eq(v, pivot)
        if op == "<>":
            return not self.eq(v, pivot)
        if op == "IN":
            return any(self.eq(v, p) for p in pivot)
        if op == "BETWEEN":
            return not self.lt(v, pivot[0]) and not self.lt(pivot[1], v)
        if op == "LIKE":
            rx = "".join(".*" if c == "%" else re.escape(c) for c in pivot)
            return isinstance(v, str) and re.fullmatch(rx, v, re.S) is not None
        if op == "CONTAINS":
            return isinstance(v, list) and any(self.eq(x, pivot) for x in v)
        if op == "CONTAINSANY":
            return isinstance(v, list) and any(self.eq(x, p) for p in pivot for x in v)
        if op == "CONTAINSALL":
            return isinstance(v, list) and all(any(self.eq(x, p) for x in v) for p in pivot)
        if op == "<":
            return self.lt(v, pivot)
        if op == "<=":
            return not self.lt(pivot, v)
        if op == ">":
            return self.lt(pivot, v)
        if op == ">=":
            return not self.lt(v, pivot)
        raise ValueError(op)

    def evaluate(self, values, p) -> bool:
        if isinstance(p, Compound):
            if p.op == "NOT":
                return not self.evaluate(values, p.children[0])
            return (all if p.op == "AND" else any)(self.evaluate(values, c) for c in p.children)
        if p.sub is not None:
            return self.predicate_hit(p.field in values, values.get(p.field), "IN", p.pivot)
        return self.predicate_hit(p.field in values, values.get(p.field), p.op, p.pivot, p.ftype, p.ci, p.key)

    def apply_arith(self, v, sym, c, ftype):
        return {"+": v + c, "-": v - c, "*": v * c}[sym]

    def agg(self, fn, values, ftype=None):
        present = [v for v in values if v is not None]
        if fn == "count":
            return len(values)
        if fn == "distinct_count":
            return len({self.key(v) for v in values})
        if fn == "sum":
            return sum(present) if present else 0
        if not present:
            return None
        return (max if fn == "max" else min)(present, key=self.key)

    def mean(self, values):
        return sum(values) / len(values)

    def sum_is_exact(self, values, ftype="int") -> bool:
        return all(isinstance(v, int) and not isinstance(v, bool) for v in values)

    def aggregate_over_no_rows(self, fn) -> list:
        return [{"v": None}]

    def nulls_first(self, desc: bool) -> bool:
        return False

    def order_key(self, v, desc: bool):
        if v is None:
            return (1, ())
        k = self.key(v)
        return (0, tuple(-x if isinstance(x, (int, float)) else x for x in k)) if desc else (0, k)

    def orders_exactly(self, ftype, custom_keys: bool) -> bool:
        return True


# --- the "engine": one Instance copy per database --------------------------------------------


@step("fake_load")
@dataclass
class Load:
    """The whole setup of a data model: hold this Instance."""

    instance: object

    def to_dict(self) -> dict:
        return {"op": "fake_load", "nodes": len(self.instance.nodes), "edges": len(self.instance.edges)}

    @classmethod
    def from_dict(cls, d: dict) -> "Load":
        raise NotImplementedError("a fake plan is not replayable from its JSON")


def _rows(answer) -> list:
    if isinstance(answer, dict):
        return [{"id": k, "v": v} for k, v in answer.items()]
    return list(answer)


class FakeSession(Session):
    def __init__(self):
        self.instance = None
        # tests set this to corrupt what one data model answers
        self.tamper = None

    def apply_setup(self, plan: list) -> None:
        for s in plan:
            self.instance = s.instance

    def execute(self, text: str, binds=None, options=None) -> list:
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise EngineError(f"not a query: {e}") from e
        kind, params = doc["ast"], doc["q"]
        if kind == "traversal":
            q = traversal_ast.from_params(params)
            value = traversal_ast.evaluate(q, self.instance)
            rows = [{"id": i} for i in value] if q.projection in traversal_ast.SET_PROJECTIONS else _rows(value)
        elif kind == "select":
            rows = _rows(select_ast.evaluate(select_ast.from_params(params), self.instance))
        elif kind == "path":
            rows = _rows(path_ast.evaluate(path_ast.from_params(params), self.instance))
        else:
            raise EngineError(f"unknown query kind {kind!r}")
        return self.tamper(rows) if self.tamper else rows

    def read_nodes(self, table: str, ids=None) -> dict:
        rows = {n.record_id: dict(n.values) for n in self.instance.nodes_of(table)}
        return rows if ids is None else {rid: r for rid, r in rows.items() if rid in set(ids)}

    def read_edges(self, data_model: str, instance, edge_type, sources=None) -> list:
        edges = self.instance.edges_of(edge_type.name)
        if sources is not None:
            wanted = set(sources)
            edges = [e for e in edges if e.from_record in wanted]
        return [(e.record_id, e.from_record, e.to_record, {k: v for k, v in e.values.items() if v is not None}) for e in edges]

    def ping(self):
        return [{"ok": 1}]


class FakeServer(Server):
    def __init__(self):
        self.databases: dict = {}
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def fresh_session(self, database: str) -> FakeSession:
        self.databases[database] = FakeSession()
        return self.databases[database]

    def drop_database(self, database: str) -> None:
        self.databases.pop(database, None)


# --- concrete syntax: the IR as JSON ------------------------------------------------------


def _text(kind: str, params: dict) -> str:
    return json.dumps({"ast": kind, "q": params}, sort_keys=True, default=repr)


class _TraversalSyntax:
    @staticmethod
    def syntaxes_for(q, schema, data_model) -> list:
        return ["json"]

    @staticmethod
    def render(q, schema, data_model, syntax, pull_up=False) -> str:
        return _text("traversal", q.to_params())

    @staticmethod
    def can_pull_up(q) -> bool:
        return False


@dataclass(frozen=True)
class _Syntax:
    tag: str = ""
    axes: int = 0


@dataclass
class _NoRewrites:
    rw: object = None
    rev: object = None
    ar: object = None


class _SelectSyntax:
    BASE = _Syntax()
    METHODS: dict = {}

    @staticmethod
    def method_fn(template, ftype):
        raise KeyError(template)

    @staticmethod
    def build_rewrites(q, rng, schema, instance):
        return _NoRewrites()

    @staticmethod
    def extra_binds(q, schema, rewrites) -> dict:
        return {}

    @staticmethod
    def rewrite_params(rewrites) -> dict:
        return {}

    @staticmethod
    def base_data_models(q) -> tuple:
        return DATA_MODELS

    @staticmethod
    def syntaxes_for(q, schema, rewrites, data_model) -> list:
        return []

    @staticmethod
    def applicable(q, schema, syntax, rewrites, data_model) -> bool:
        return True

    @staticmethod
    def render(q, schema, syntax, rewrites, data_model) -> str:
        return _text("select", q.to_params())


class _PathSyntax:
    @staticmethod
    def render_all(q, schema, instance) -> tuple:
        return {data_model: _text("path", q.to_params()) for data_model in DATA_MODELS}, {}

    @staticmethod
    def bind_vars(q) -> dict:
        return {}


@dataclass
class _Statements:
    query: object
    text: str
    main: tuple = ("write", {})
    params: dict = field(default_factory=dict)

    def display(self) -> str:
        return self.text


class _WriteSyntax:
    @staticmethod
    def build_rewrites(q, rng, schema, instance):
        return _NoRewrites()

    @staticmethod
    def all_syntaxes(q, data_model) -> list:
        return [_Syntax("base")]

    @staticmethod
    def applicable(q, syntax, rewrites, data_model) -> bool:
        return True

    @staticmethod
    def render(q, syntax, rewrites, data_model, before, after) -> _Statements:
        return _Statements(q, _text("write", q.to_params()))

    @staticmethod
    def execute(session, st) -> list:
        answer = write_ast.answer(st.query, session.instance)
        session.instance = write_ast.apply(st.query, session.instance)
        return answer

    @staticmethod
    def shared_binds(st) -> dict:
        return {}

    @staticmethod
    def rewrite_params(rewrites) -> dict:
        return {}


class _Pivots:
    NULL_OPS = ("IS NULL", "IS NOT NULL")
    ORDERED_OPS = ("=", "<>", ">", ">=", "<", "<=", "IN", "BETWEEN")
    LIKE_WILDCARDS = "%"

    @staticmethod
    def ordered_ops(ftype):
        return _Pivots.ORDERED_OPS

    @staticmethod
    def cross_type(rng, ftype, v):
        return v

    @staticmethod
    def refine(rng, ftype, v):
        return v


class _PhysicalDesign:
    BOUNDARY_VALUES = BOUNDARY_VALUES
    NON_UNIQUE_KINDS = ("plain",)
    EDGE_INDEX_DATA_MODELS = ("graph", "rel", "both")

    @staticmethod
    def draw_index_kind(rng) -> str:
        return "plain"

    @staticmethod
    def is_unique(ix) -> bool:
        return False

    @staticmethod
    def holds_one_record_per_key(ix) -> bool:
        return False

    @staticmethod
    def demoted_kind(kind: str) -> str:
        return kind

    @staticmethod
    def index_key_value(v, ci=False):
        return json.dumps(v, sort_keys=True, default=repr)

    @staticmethod
    def draw_id_indexes(seed, cfg) -> dict:
        return {}

    @staticmethod
    def draw_storage_options(seed, cfg, node_types):
        return node_types

    @staticmethod
    def draw_write_form(rng) -> WriteForm:
        return WriteForm()

    @staticmethod
    def is_valid_key(key: str) -> bool:
        return True


class _Rewrites:
    @staticmethod
    def arith_exact(v, sym, c, ftype) -> bool:
        return isinstance(v, int) and not isinstance(v, bool) and abs(v) < 2**40


class _KnownBugs:
    @staticmethod
    def mark_known_bugs(case, q, schema, instance):
        return case

    @staticmethod
    def select_known(q, syntax, rewrites, data_model, schema, instance):
        return None

    @staticmethod
    def write_known(q, syntax, rewrites, data_model, schema, instance):
        return None


class FakeProvider(Provider):
    name = "fake"
    title = "Fake"
    data_models = DATA_MODELS
    config_class = GeneratorConfig
    # small bounds, so a large-scale test stays quick and every cap is reached
    limits = StorageLimits(
        max_string_chars=20000,
        max_binary_bytes=20000,
        max_list_items=2000,
        max_fields_per_record=40,
        max_record_bytes=64 * 1024,
        max_rows_per_table=100000,
        max_edges_per_node=50,
        max_index_key_bytes=1024,
        max_request_bytes=256 * 1024,
        max_statements_per_request=50,
    )
    semantics = FakeSemantics()

    pivots = _Pivots
    physical_design = _PhysicalDesign
    mappers = {data_model: (lambda instance: [Load(dataclasses.replace(instance))]) for data_model in DATA_MODELS}
    traversal_syntax = _TraversalSyntax
    select_syntax = _SelectSyntax
    path_syntax = _PathSyntax
    write_syntax = _WriteSyntax
    rewrites = _Rewrites
    known_bugs = _KnownBugs
    mutants = INSTANCE_MUTANTS

    def __init__(self):
        self.servers: list = []

    def server(self, args=None) -> FakeServer:
        self.servers.append(FakeServer())
        return self.servers[-1]


SMALL = dict(max_node_types=3, max_edge_types=4, min_nodes_per_type=3, max_nodes_per_type=8, min_edges_per_type=3, max_edges_per_type=10, traversal_cases=8, select_cases=20, path_cases=2, write_cases=3)
