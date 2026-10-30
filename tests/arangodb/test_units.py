"""The ArangoDB provider without a server: the key encoding, the value semantics, the
rewrites' equivalence under them, every plan's round trip and offline replay against
the Instance, and that every case and every write renders."""
from __future__ import annotations

import copy
import dataclasses
import random
import re

import pytest

from ganda.core.query.ir.predicates import Compound, Predicate
from ganda.core.query.ir.write import apply
from ganda.core.data.generator import build
from ganda.core.query.generators.registry import all_cases
from ganda.core.query.generators.write import sample_write
from ganda.core.data.model import FieldType
from ganda.core.engine.setup_plan import plan_from_dict, plan_to_dict
from ganda.core.engine.state_check import compare_states
from tests.instance_session import InstanceSession
from ganda.arangodb.concrete_syntax.query.predicate_sampling.forms import _arithmetic, leaf_form
from ganda.arangodb.engine.semantics import arith_exact
from ganda.arangodb.engine.connection import bind_subset
from ganda.arangodb.engine.dialect import ARANGO_KEY_RE, document_key, engine_id, logical_id
from ganda.arangodb.engine.plan import Aql, CreateCollection, CreateIndex, DocumentApi, Insert, Transaction
from ganda.arangodb.concrete_syntax.setup.common import chunks
from ganda.arangodb.provider import PROVIDER as E
from ganda.arangodb.engine.semantics import aql_key, eq, lt
from ganda.arangodb.concrete_syntax.query.predicate_sampling.predicate_steering import evaluate, like_hit, predicate_hit
from ganda.arangodb.engine import tx

CFG = E.config_class(custom_key_probability=0.3, inheritance_probability=0.5, narrow_type_probability=0.3, date_map_probability=0.5, collate_probability=0.4, list_index_probability=0.5, churn_edge_update_probability=0.2, write_form_probability=0.8, traversal_cases=6, select_cases=16, path_cases=2, write_cases=3)
SEEDS = (1, 2, 3, 4)


# --- the dialect ------------------------------------------------------------------------------


def test_document_keys_are_engine_keys_and_invert():
    for key in ("r0", "a b", 'q"q', "%", "k:1", "é", "\U0001F600", "it's", "k" * 190 + "7", "a~b", "42"):
        k = document_key(key)
        assert ARANGO_KEY_RE.match(k), key
        assert logical_id(engine_id(f"n1/{key}")) == f"n1/{key}"
    assert document_key("r0") == "r0" and document_key("a b") == "a%20b" and document_key("%") == "%25"


def test_bind_subset_keeps_the_names_a_text_uses():
    assert bind_subset("FILTER n.f == @p1 AND IN_RANGE(n.g, @p2a, @p2b, true, true)", {"p1": 1, "p2a": 2, "p2b": 3, "p3": 4}) == {"p1": 1, "p2a": 2, "p2b": 3}
    assert bind_subset("FOR d IN @@coll RETURN d", {"@coll": "n1", "x": 1}) == {"@coll": "n1"}


def test_the_door_script_round_trips():
    text = tx.script({"write": ["n1"], "read": ["n1", "e1"]}, ["FOR x IN n1 REMOVE x IN n1"], "FOR n IN n1 RETURN n")
    assert tx.is_script(text)
    assert tx.parts(text) == ({"read": ["n1", "e1"], "write": ["n1"]}, ["FOR x IN n1 REMOVE x IN n1"], "FOR n IN n1 RETURN n")


# --- the semantics ----------------------------------------------------------------------------


def test_the_total_type_order():
    assert lt(None, False) and lt(True, 0) and lt(5, "a") and lt("z", []) and lt([9], {})
    assert eq(1, 1.0) and eq(-0.0, 0) and eq(None, None) and not eq(True, 1) and not eq("1", 1)
    assert not eq(2**53 + 1, 9007199254740992.0) and not eq(2**53 + 1, 2**53) and lt(9007199254740992.0, 2**53 + 1)
    assert not lt(None, None) and not lt({}, {})
    assert aql_key(1) == aql_key(1.0) and aql_key(-0.0) == aql_key(0.0)
    assert predicate_hit(False, None, "<", 5) and predicate_hit(True, None, "<=", 5) and not predicate_hit(False, None, ">", 5)
    assert predicate_hit(False, None, "<>", 5) and not predicate_hit(False, None, "=", 5)
    assert predicate_hit(True, None, "IN", [1, None]) and not predicate_hit(True, 2, "IN", [1, None])
    assert not predicate_hit(False, None, "BETWEEN", [1, 5]) and predicate_hit(True, 3, "BETWEEN", [1, 5])
    assert predicate_hit(True, [1, 2], "HAS ALL", [1]) and not predicate_hit(True, None, "HAS", 1)
    assert predicate_hit(True, {"k1": 2}, "HAS KEY", "k1") and predicate_hit(True, {"k1": 2}, "VALUES HAS", 2)
    assert predicate_hit(True, {"k1": 2}, ">", 1, key="k1") and predicate_hit(True, {"k2": 2}, "<", 1, key="k1")


def test_like_is_case_sensitive_and_crosses_a_newline_but_not_a_paragraph_separator():
    assert like_hit("abc", "a%") and not like_hit("Abc", "a%") and like_hit("abc", "a_c") and not like_hit("abc", "a_")
    assert like_hit("a\nb", "a%") and like_hit("a\nb", "a__") and not like_hit("ab\n", "ab") and not like_hit("ab\n", "%b")
    assert not like_hit("a\u2028b", "a%") and like_hit("a%b", "a\\%b") and not like_hit("axb", "a\\%b")
    assert like_hit(None, "%") and not like_hit(None, "a%")


def _leaf(field, op, constant=None, ftype="int"):
    return Predicate("source", field, op, "" if op in ("IS NULL", "IS NOT NULL", "IS PRESENT", "IS ABSENT") else f"b{field}", constant, ftype)


def _random_tree(rng, depth=0):
    fields = {"a": FieldType.INT, "b": FieldType.INT, "l": FieldType.LIST, "m": FieldType.MAP}
    if depth < 2 and rng.random() < 0.5:
        op = rng.choice(("AND", "OR", "NOT"))
        kids = (_random_tree(rng, depth + 1),) if op == "NOT" else (_random_tree(rng, depth + 1), _random_tree(rng, depth + 1))
        return Compound("source", op, kids)
    f = rng.choice(list(fields))
    if fields[f] is FieldType.LIST:
        op = rng.choice(("HAS", "HAS ANY", "HAS ALL", "=", "IS NULL"))
        constant = {"HAS": rng.randint(0, 3), "=": [rng.randint(0, 3)], "IS NULL": None}.get(op, [rng.randint(0, 3) for _ in range(2)])
        return _leaf(f, op, constant, "list")
    if fields[f] is FieldType.MAP:
        op = rng.choice(("HAS KEY", "VALUES HAS", "IS NOT NULL"))
        return _leaf(f, op, {"HAS KEY": rng.choice(("k1", "k2")), "VALUES HAS": rng.randint(0, 3)}.get(op), "map")
    op = rng.choice(("=", "<>", "<", "<=", ">", ">=", "IN", "BETWEEN", "IS NULL", "IS NOT NULL", "IS PRESENT", "IS ABSENT"))
    constant = {"IN": [rng.randint(0, 3), rng.randint(0, 3)], "BETWEEN": sorted([rng.randint(0, 3), rng.randint(0, 3)])}.get(op, rng.randint(0, 3) if not op.startswith("IS") else None)
    return _leaf(f, op, constant)


def _records():
    values = [None, 0, 1, 2, 3]
    out = []
    for a in values + ["absent"]:
        for b in (None, 2, "absent"):
            for l in (None, [], [1], [1, 2, 3], "absent"):
                for m in (None, {}, {"k1": 1}, {"k2": 0}, "absent"):
                    r = {}
                    if a != "absent":
                        r["a"] = a
                    if b != "absent":
                        r["b"] = b
                    if l != "absent":
                        r["l"] = l
                    if m != "absent":
                        r["m"] = m
                    out.append(r)
    return out


def test_the_arithmetic_form_shifts_the_constant_alike():
    rng = random.Random(3)
    values = [None, 0, 1, -5, 7, 2**40]
    seen = 0
    for op in ("=", "<>", "<", "<=", ">", ">="):
        p = _leaf("a", op, 7)
        for _ in range(20):
            ar = _arithmetic(rng, p)
            if ar is None:
                continue
            seen += 1
            # the same records wherever the field is a number (a null operand is 0 in AQL)
            for v in values[1:]:
                assert evaluate({"a": v}, p) == evaluate({"a": v}, ar), (op, ar, v)
            assert evaluate({}, ar) == evaluate({"a": 0}, ar)
    assert seen > 20
    assert not arith_exact(2**53, "+", 1, FieldType.INT) and arith_exact(0.5, "*", 2, FieldType.FLOAT) and not arith_exact(0.5, "+", 1, FieldType.FLOAT)


def test_leaf_forms():
    class Roll:
        def __init__(self, *rolls):
            self.rolls = list(rolls)

        def random(self):
            return self.rolls.pop(0) if self.rolls else 0.99

        def choice(self, xs):
            return xs[0]

    from ganda.arangodb.concrete_syntax.query.predicate import render_predicate

    p = _leaf("a", "<", 7)
    assert render_predicate(leaf_form(Roll(0.05), p)) == "@ba > n.a"
    assert leaf_form(Roll(0.15), p).arith is not None
    assert leaf_form(Roll(0.5), p) == p
    m = Predicate("source", "m", "=", "p", 2, "int", False, "k2")
    assert render_predicate(leaf_form(Roll(0.1), m)) == "n.m[@pk] == @p"


# --- the plans: an offline engine replaying every step ----------------------------------------


_KEY_UPDATE = re.compile(r"^UPDATE @key WITH @doc IN (\w+)(?: OPTIONS \{([^}]*)\})?$")
_KEY_REPLACE = re.compile(r"^REPLACE @key WITH @doc IN (\w+)$")
_UPSERT = re.compile(r"^UPSERT \{_key: @key\} INSERT @ins UPDATE @doc IN (\w+)(?: OPTIONS \{([^}]*)\})?$")
_OVERWRITE = re.compile(r"^INSERT @doc INTO (\w+) OPTIONS \{overwriteMode: 'replace'\}$")
_INSERT = re.compile(r"^INSERT @doc INTO (\w+)$")
_REMOVE = re.compile(r"^REMOVE @key IN (\w+)$")


class _Store:
    """The documents of one database as the plan's statements leave them."""

    def __init__(self):
        self.colls: dict = {}

    def _merge(self, doc: dict, patch: dict, keep_null: bool, merge_objects: bool = True) -> None:
        """AQL's UPDATE: a null unsets under `keepNull: false`; a map value is merged
        key by key into the stored one unless `mergeObjects: false`."""
        for k, v in patch.items():
            if v is None and not keep_null:
                doc.pop(k, None)
            elif merge_objects and isinstance(v, dict) and isinstance(doc.get(k), dict):
                self._merge(doc[k], v, keep_null, merge_objects)
            else:
                doc[k] = copy.deepcopy(v)

    @staticmethod
    def _options(text) -> tuple:
        opts = text or ""
        return "keepNull: false" not in opts, "mergeObjects: false" not in opts

    def step(self, s) -> None:
        if isinstance(s, CreateCollection):
            self.colls[s.name] = {}
        elif isinstance(s, CreateIndex):
            assert s.collection in self.colls
        elif isinstance(s, Insert):
            for d in s.documents:
                assert d["_key"] not in self.colls[s.collection], d["_key"]
                self.colls[s.collection][d["_key"]] = copy.deepcopy(d)
        elif isinstance(s, Aql):
            self.aql(s.query, s.bind_vars)
        elif isinstance(s, DocumentApi) and not s.key:
            for body in s.body:
                self.step(DocumentApi(s.method, s.collection, body["_key"], {k: v for k, v in body.items() if k != "_key"}, s.params))
        elif isinstance(s, DocumentApi):
            coll = self.colls[s.collection]
            if s.method == "PATCH":
                self._merge(coll[s.key], s.body, s.params.get("keepNull", "true") != "false", s.params.get("mergeObjects", "true") != "false")
            elif s.method == "PUT":
                coll[s.key] = {"_key": s.key, **copy.deepcopy(s.body)}
            else:
                del coll[s.key]
        elif isinstance(s, Transaction):
            saved = copy.deepcopy(self.colls)
            for inner in s.steps:
                self.step(inner)
            if not s.commit:
                self.colls = saved
        else:
            raise TypeError(s)

    def aql(self, text: str, b: dict) -> None:
        if text.startswith("FOR r IN @rows "):
            inner = re.sub(r"\br\.([A-Za-z_][A-Za-z0-9_]*)", r"@\1", text[len("FOR r IN @rows "):])
            for row in b["rows"]:
                self.aql(inner, row)
            return
        if m := _KEY_UPDATE.match(text):
            self._merge(self.colls[m.group(1)][b["key"]], b["doc"], *self._options(m.group(2)))
        elif m := _KEY_REPLACE.match(text):
            self.colls[m.group(1)][b["key"]] = {"_key": b["key"], **copy.deepcopy(b["doc"])}
        elif m := _UPSERT.match(text):
            coll = self.colls[m.group(1)]
            if b["key"] in coll:
                self._merge(coll[b["key"]], b["doc"], *self._options(m.group(2)))
            else:
                coll[b["key"]] = copy.deepcopy(b["ins"])
        elif m := _OVERWRITE.match(text):
            self.colls[m.group(1)][b["doc"]["_key"]] = copy.deepcopy(b["doc"])
        elif m := _INSERT.match(text):
            assert b["doc"]["_key"] not in self.colls[m.group(1)]
            self.colls[m.group(1)][b["doc"]["_key"]] = copy.deepcopy(b["doc"])
        elif m := _REMOVE.match(text):
            del self.colls[m.group(1)][b["key"]]
        else:
            raise ValueError(text)


class _Session:
    """`state_check.compare_states`'s view of a `_Store`."""

    def __init__(self, store: _Store):
        self.store = store

    def read_nodes(self, table, ids=None):
        rows = {d["id"]: d for d in self.store.colls[table].values()}
        return rows if ids is None else {k: v for k, v in rows.items() if k in set(ids)}

    def read_edges(self, data_model, instance, et, sources=None):
        out = []
        if data_model == "doc":
            for d in self.store.colls[et.from_type].values():
                for link in d.get(et.name) or []:
                    out.append((link["eid"], d["id"], logical_id(link["target"]), {k: v for k, v in link.items() if k not in ("eid", "target") and v is not None}))
        elif data_model == "graph":
            for d in self.store.colls[et.name].values():
                out.append((d["eid"], logical_id(d["_from"]), logical_id(d["_to"]), {k: v for k, v in d.items() if k not in ("_key", "_from", "_to", "eid") and v is not None}))
        else:
            for d in self.store.colls[et.name].values():
                out.append((d["eid"], d["src_id"], d["dst_id"], {k: v for k, v in d.items() if k not in ("_key", "eid", "src_id", "dst_id") and v is not None}))
        if sources is not None:
            out = [x for x in out if x[1] in set(sources)]
        return out


BULK_CFG = dataclasses.replace(CFG, statements_per_request=7, write_form_probability=1.0)


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("cfg", [CFG, BULK_CFG], ids=["plain", "bulk"])
def test_every_plan_round_trips_and_replays_to_the_instance(seed, cfg):
    schema, instance, _rng = build(seed, cfg, E)
    sessions = {"instance": InstanceSession(instance)}
    for data_model in E.data_models:
        plan = E.mappers[data_model](instance)
        if cfg is BULK_CFG:
            folded = [s for s in _flat(plan) if isinstance(s, Aql) and s.query.startswith("FOR r IN @rows ")]
            assert all(len(s.bind_vars["rows"]) <= 7 for s in folded)
            assert all(len({r.get("key") for r in s.bind_vars["rows"]}) == len(s.bind_vars["rows"]) for s in folded if all("key" in r for r in s.bind_vars["rows"]))
        assert plan_to_dict(plan_from_dict(plan_to_dict(plan))) == plan_to_dict(plan)
        store = _Store()
        for s in plan:
            store.step(s)
        sessions[data_model] = _Session(store)
        # the cancelled junk left nothing behind
        assert not any("junk" in k for coll in store.colls.values() for k in coll)
    for data_model, diff in compare_states(sessions, instance).items():
        assert diff.ok, (data_model, diff.problems[:5])


def _flat(plan) -> list:
    out = []
    for s in plan:
        out.extend(_flat(s.steps) if isinstance(s, Transaction) else [s])
    return out


def test_folding_keeps_each_documents_statement_order_and_bounds_a_transaction():
    from ganda.arangodb.concrete_syntax.setup.common import fold_steps, transaction_groups

    steps = [Aql("UPDATE @key WITH @doc IN n1 OPTIONS {keepNull: false, mergeObjects: false}", {"key": "r1", "doc": {"a": None}}), Aql("UPDATE @key WITH @doc IN n1 OPTIONS {mergeObjects: false}", {"key": "r1", "doc": {"b": 1}}), Aql("UPDATE @key WITH @doc IN n1 OPTIONS {keepNull: false, mergeObjects: false}", {"key": "r2", "doc": {"a": None}}), Aql("UPDATE @key WITH @doc IN n1 OPTIONS {mergeObjects: false}", {"key": "r2", "doc": {"b": 2}}), Aql("UPDATE @key WITH @doc IN n1 OPTIONS {mergeObjects: false}", {"key": "r2", "doc": {"e1": []}}), Aql("REMOVE @key IN n1", {"key": "r3"}), Aql("REMOVE @key IN n1", {"key": "r4"})]
    folded = fold_steps(steps, 10)
    assert [s.query.split(" ")[4] if s.query.startswith("FOR r") else s.query.split(" ")[0] for s in folded] == ["UPDATE", "UPDATE", "UPDATE", "REMOVE"]
    assert folded[0].bind_vars["rows"] == [{"key": "r1", "doc": {"a": None}}, {"key": "r2", "doc": {"a": None}}]
    # the same document twice closes a bulk statement, so its two writes stay ordered
    assert [r["key"] for r in folded[1].bind_vars["rows"]] == ["r1", "r2"] and folded[2].bind_vars == {"key": "r2", "doc": {"e1": []}}
    assert folded[3].query == "FOR r IN @rows REMOVE r.key IN n1"
    groups = transaction_groups([Insert("n1", [{"_key": str(i)} for i in range(6)]), Insert("n1", [{"_key": "x"}])] + folded, 7)
    assert [sum(len(getattr(s, "documents", [])) or len(s.bind_vars.get("rows", [1])) for s in g) for g in groups] == [7, 7]


def test_bulk_inserts_are_chunked_by_count_and_by_bytes():
    docs = [{"_key": f"r{i}", "s": "x" * 1000} for i in range(50)]
    assert [len(c) for c in chunks(docs, 20)] == [20, 20, 10]
    assert [len(c) for c in chunks(docs, 0)] == [50]
    big = [{"_key": f"r{i}", "s": "x" * (20 * 1024 * 1024)} for i in range(3)]
    assert [len(c) for c in chunks(big, 0)] == [1, 1, 1]


# --- every case and every write renders -------------------------------------------------------


@pytest.mark.parametrize("seed", SEEDS)
def test_every_case_renders_with_its_binds_named(seed):
    schema, instance, rng = build(seed, CFG, E)
    cases = all_cases(rng, schema, instance, cfg=CFG)
    assert cases
    for c in cases:
        for variant, text in c.queries.items():
            assert text and "None" not in text, (variant, text)
            if tx.is_script(text):
                _colls, statements, question = tx.parts(text)
                assert statements and question
            # every bind variable the text names is carried, under its own name
            for name in re.findall(r"@([A-Za-z_][A-Za-z0-9_]*)", text):
                assert name in c.bind_vars, (variant, name, text)


@pytest.mark.parametrize("seed", SEEDS)
def test_every_write_renders_on_every_data_model(seed):
    schema, instance, rng = build(seed, CFG, E)
    inst = instance
    rendered = 0
    for step in range(6):
        drawn = sample_write(rng.for_generator("ir_write"), schema, inst, step)
        if drawn is None:
            continue
        q, rw, syntaxes = drawn
        after = apply(q, inst)
        for data_model in E.data_models:
            st = E.write_syntax.render(q, syntaxes[data_model][0], rw, data_model, inst, after)
            assert st.main is not None or (q.edge and data_model == "doc")
            for text, params in st.prelude + ([st.main] if st.main else []):
                for name in re.findall(r"@([A-Za-z_][A-Za-z0-9_]*)", text):
                    assert name in params, (data_model, name, text)
        inst = after
        rendered += 1
    assert rendered >= 3


_FORM_KINDS = {"order": lambda sel: sel["kind"] == "agg" and sel["fn"] in ("max", "min"), "set": lambda sel: sel["kind"] == "agg" and sel["fn"] == "distinct_count", "fn": lambda sel: sel["kind"] == "agg" and sel["fn"] == "distinct_count", "collect": lambda sel: sel["kind"] in ("count", "group", "distinct") or (sel["kind"] == "agg" and sel["fn"] in ("max", "min", "count", "avg", "sum")), "jrev": lambda sel: sel["kind"] == "join"}


@pytest.mark.parametrize("seed", SEEDS)
def test_every_select_variant_form_fits_its_kind(seed):
    schema, instance, rng = build(seed, CFG, E)
    seen = set()
    for c in all_cases(rng, schema, instance, cfg=CFG):
        if c.generator_name != "ir_select":
            continue
        for variant in c.queries:
            for part in variant.partition(":")[2].split("+"):
                if part in _FORM_KINDS:
                    assert _FORM_KINDS[part](c.params["select"]), (variant, c.params["select"])
                    seen.add(part)


# --- the known bug A42 ---------------------------------------------------------------------------


def test_a42_guard_fires_on_a_like_wildcard_over_a_long_stored_string():
    from ganda.core.data.model import NodeRow
    from ganda.arangodb.known_bugs.predicate import LIKE_WILDCARD_MAX_CHARS, a42_shape, like_wildcard_fields

    infix = Predicate("source", "s", "LIKE", "p", "%m%", "string")
    exact = Predicate("source", "s", "LIKE", "p", "abc", "string")
    other = Predicate("source", "a", "=", "q", 1, "int")
    assert like_wildcard_fields(infix) == {"s"}
    assert like_wildcard_fields(Compound("source", "NOT", (Compound("source", "AND", (other, infix)),))) == {"s"}
    assert like_wildcard_fields(exact) == set() and like_wildcard_fields(other) == set() and like_wildcard_fields(None) == set()
    long, short = NodeRow("n1", 0, {"s": "\U0001F600" * LIKE_WILDCARD_MAX_CHARS}), NodeRow("n1", 1, {"s": "a" * (LIKE_WILDCARD_MAX_CHARS - 1)})
    assert a42_shape(infix, [short, long])
    assert not a42_shape(infix, [short])  # under the bound
    assert not a42_shape(exact, [long])  # no wildcard
    assert not a42_shape(Predicate("source", "t", "LIKE", "p", "%m%", "string"), [long])  # another column
    assert not a42_shape(infix, [NodeRow("n1", 2, {"s": None}), NodeRow("n1", 3, {})])


def test_a42_withholds_every_select_variant_and_a_traversals_pulled_up_form():
    from ganda.arangodb.concrete_syntax.query.select import Rewrites, SelectSyntax
    from ganda.arangodb.known_bugs import mark_known_bugs, select_known
    from ganda.arangodb.known_bugs.predicate import LIKE_WILDCARD_MAX_CHARS
    from ganda.core.data.model import EdgeTypeSpec, FieldSpec, NodeTypeSpec, NodeRow
    from ganda.core.oracle.case import Comparison, QueryCase
    from ganda.core.query.ir.select import SelectQuery
    from ganda.core.query.ir.traversal import Hop, TraversalQuery

    schema = E.schema(node_types=(NodeTypeSpec("n1", (FieldSpec("s", FieldType.STRING),)),), edge_types=(EdgeTypeSpec("e1", "n1", "n1", ()),))
    like = Predicate("source", "s", "LIKE", "p_0", "ab%", "string")
    inst = build(1, CFG, E)[1]
    inst = dataclasses.replace(inst, schema=schema, nodes=[NodeRow("n1", 0, {"s": "a" * LIKE_WILDCARD_MAX_CHARS}), NodeRow("n1", 1, {"s": "ab"})], edges=[], churn=[])
    q = SelectQuery("n1", like, "ids", ())
    rw = Rewrites()
    for sp in (SelectSyntax(), SelectSyntax(form="collect")):
        assert select_known(q, sp, rw, "doc", schema, inst) == "A42"
    assert select_known(SelectQuery("n1", Predicate("source", "s", "LIKE", "p_0", "ab", "string"), "ids", ()), SelectSyntax(), rw, "doc", schema, inst) is None
    tq = TraversalQuery("n1", (Hop("e1", "out"),), "ids", source_predicate=like)
    case = QueryCase("ir_traversal", "", Comparison.SCALAR_BY_ID, {"doc": "w", "doc:pull_up": "x", "graph": "y", "rel": "z"}, {}, {"pulled_up": ["doc:pull_up"]}, shape="s")
    assert mark_known_bugs(case, tq, schema, inst).known_bugs == {"doc": "A42", "doc:pull_up": "A42", "graph": "A42", "rel": "A42"}


# --- the mutation catalog -------------------------------------------------------------------------


def test_every_mutant_corrupts_something():
    from ganda.arangodb.mutation.mutants import MUTANTS

    assert len({m.name for m in MUTANTS}) == len(MUTANTS)
    cfg = dataclasses.replace(CFG, index_probability=0.9, composite_index_probability=0.5, churn_update_probability=0.5, churn_delete_probability=0.4, churn_node_delete_probability=0.4)
    changed = {m.name: False for m in MUTANTS}
    # a range on an indexed field is rare: the case mutants get more seeds, until each fires
    for seed in range(1, 25):
        if seed > max(SEEDS) and all(changed.values()):
            break
        schema, inst, rng = build(seed, cfg, E)
        for m in MUTANTS:
            for fn in m.instance_for_data_model.values():
                out = fn(inst)
                changed[m.name] |= out.edges != inst.edges or out.nodes != inst.nodes or out.churn != inst.churn
        if not any(m.rewrite_case or m.rewrite_case_schema for m in MUTANTS):
            continue
        for case in all_cases(rng, schema, inst, cfg=cfg):
            for m in MUTANTS:
                if m.rewrite_case is not None:
                    changed[m.name] |= m.rewrite_case(case).queries != case.queries
                if m.rewrite_case_schema is not None:
                    changed[m.name] |= m.rewrite_case_schema(case, schema).queries != case.queries
    assert all(changed.values()), [n for n, c in changed.items() if not c]


def test_index_mutants_leave_the_transaction_door_alone():
    from ganda.arangodb.mutation.mutants import _desc_as_asc, _inclusive_range, _index_served, _null_keys

    assert _inclusive_range("FOR n IN n1 FILTER n.f > @p RETURN {id: n.id}", ["f"]) == "FOR n IN n1 FILTER n.f >= @p RETURN {id: n.id}"
    assert _inclusive_range("FOR n IN n1 FILTER n.g > @p RETURN {id: n.id}", ["f"]) == "FOR n IN n1 FILTER n.g > @p RETURN {id: n.id}"
    assert _inclusive_range("FOR n IN n1 FILTER n.f >= @p RETURN 1", ["f"]) == "FOR n IN n1 FILTER n.f > @p RETURN 1"
    assert _null_keys("FOR n IN n1 FILTER n.f == @p RETURN {id: n.id}", ["f"]) == "FOR n IN n1 FILTER (n.f == @p OR n.f == null) RETURN {id: n.id}"
    assert _null_keys("FOR n IN n1 FILTER n.f IN @p RETURN 1", ["f"]) == "FOR n IN n1 FILTER (n.f IN @p OR n.f == null) RETURN 1"
    assert _desc_as_asc("FOR n IN n1 FILTER n.f != null SORT n.f DESC LIMIT 1 RETURN {v: n.f}", ["f"]) == "FOR n IN n1 FILTER n.f != null SORT n.f ASC LIMIT 1 RETURN {v: n.f}"
    assert _index_served("FOR n IN n1 FILTER n.f > @p RETURN 1")
    assert not _index_served(tx.script({"write": ["n1"], "read": ["n1"]}, ["FOR x IN n1 REMOVE x IN n1"], "FOR n IN n1 FILTER n.f > @p RETURN 1"))


# --- the rules the generators read ----------------------------------------------------------------


def test_a_string_column_draws_the_equality_family_only_because_its_order_is_collation():
    from ganda.arangodb.concrete_syntax.query.predicate_sampling.operators import EQUALITY_OPS, ORDERED_OPS, ordered_ops

    assert ordered_ops(FieldType.STRING) == EQUALITY_OPS and ordered_ops(FieldType.BINARY) == EQUALITY_OPS
    assert ordered_ops(FieldType.INT) == ORDERED_OPS and ordered_ops(FieldType.DATE) == ORDERED_OPS


def test_the_deep_traversal_asks_every_walk_and_the_collect_form_takes_sum():
    from ganda.arangodb.concrete_syntax.query.select import _COLLECT_AGGREGATES
    from ganda.arangodb.concrete_syntax.query.traversal import _DEEP_OPTIONS

    assert _DEEP_OPTIONS == " OPTIONS {uniqueEdges: 'none'}"
    assert "sum" in _COLLECT_AGGREGATES and "max" in _COLLECT_AGGREGATES


# --- the known bug A25 ---------------------------------------------------------------------------


def test_a25_guard_fires_on_an_or_of_two_list_equalities_over_an_array_index():
    from ganda.core.data.model import EdgeTypeSpec, FieldSpec, IndexSpec, NodeTypeSpec
    from ganda.arangodb.known_bugs.predicate import a25_shape, or_of_list_equalities

    eq1 = Predicate("source", "l", "=", "p_0", [1], "list")
    eq2 = Predicate("source", "l", "=", "p_1", [66], "list")
    same = Predicate("source", "l", "=", "p_1", [1], "list")
    other = Predicate("source", "a", "=", "q", 1, "int")
    both = Compound("source", "OR", (eq1, eq2))
    assert or_of_list_equalities(both) == {"l"}
    assert or_of_list_equalities(Compound("source", "OR", (eq1, dataclasses.replace(eq2, rev=True)))) == {"l"}
    assert or_of_list_equalities(Compound("source", "AND", (both, other))) == {"l"}
    assert or_of_list_equalities(Compound("source", "OR", (eq1, eq2, Predicate("source", "l", "=", "p_2", [1, 2], "list")))) == {"l"}
    assert or_of_list_equalities(Predicate("source", "l", "IN", "p", [[1], [66]], "list")) == {"l"}
    # safe: one equality, the same value twice, a mixed OR, an OR under a NOT, a wider OR, a one-element IN
    assert or_of_list_equalities(eq1) == set()
    assert or_of_list_equalities(Compound("source", "OR", (eq1, same))) == set()
    assert or_of_list_equalities(Compound("source", "OR", (eq1, eq2, other))) == set()
    assert or_of_list_equalities(Compound("source", "NOT", (both,))) == set()
    assert or_of_list_equalities(Compound("source", "OR", (both, other))) == set()
    assert or_of_list_equalities(Predicate("source", "l", "IN", "p", [[1]], "list")) == set()
    # the data model holding the array index is the one withheld
    schema = E.schema(
        node_types=(NodeTypeSpec("n1", (FieldSpec("l", FieldType.LIST), FieldSpec("a", FieldType.INT))),),
        edge_types=(EdgeTypeSpec("e1", "n1", "n1", (FieldSpec("w", FieldType.LIST),)),),
        indexes=(IndexSpec("n1", "l", data_model="doc", kind="persistent"), IndexSpec("e1", "w", data_model="both", target="edge", kind="persistent")),
    )
    assert a25_shape(both, schema, "n1", "doc") and not a25_shape(both, schema, "n1", "graph") and not a25_shape(both, schema, "n1", "rel")
    assert not a25_shape(eq1, schema, "n1", "doc")
    edge_or = Compound("edge", "OR", (Predicate("edge", "w", "=", "p_0", [1], "list"), Predicate("edge", "w", "=", "p_1", [2], "list")))
    assert a25_shape(edge_or, schema, "e1", "graph", edge=True) and a25_shape(edge_or, schema, "e1", "rel", edge=True) and not a25_shape(edge_or, schema, "e1", "doc", edge=True)


def test_a25_withholds_the_index_servable_select_variants_only():
    from ganda.arangodb.concrete_syntax.query.select import Rewrites, SelectSyntax
    from ganda.arangodb.known_bugs import select_known
    from ganda.core.data.model import EdgeTypeSpec, FieldSpec, IndexSpec, NodeTypeSpec, NodeRow
    from ganda.core.query.ir.select import SelectQuery

    schema = E.schema(node_types=(NodeTypeSpec("n1", (FieldSpec("l", FieldType.LIST),)),), edge_types=(EdgeTypeSpec("e1", "n1", "n1", ()),), indexes=(IndexSpec("n1", "l", data_model="doc", kind="persistent"),))
    both = Compound("source", "OR", (Predicate("source", "l", "=", "p_0", [1], "list"), Predicate("source", "l", "=", "p_1", [66], "list")))
    q = SelectQuery("n1", both, "ids", ())
    inst = build(1, CFG, E)[1]
    inst = dataclasses.replace(inst, schema=schema, nodes=[NodeRow("n1", 0, {"l": [1]}), NodeRow("n1", 1, {"l": [66]})], edges=[], churn=[])
    rw = Rewrites()
    assert select_known(q, SelectSyntax(), rw, "doc", schema, inst) == "A25"
    assert select_known(q, SelectSyntax(), rw, "graph", schema, inst) is None


def test_a43_guard_names_a_strict_join_over_a_sparse_index_with_nulls():
    from ganda.arangodb.concrete_syntax.query.select import Rewrites, SelectSyntax
    from ganda.arangodb.known_bugs import select_known
    from ganda.core.data.model import EdgeTypeSpec, FieldSpec, IndexSpec, NodeRow, NodeTypeSpec
    from ganda.core.query.ir.select import SelectQuery

    sparse = IndexSpec("n1", "f", data_model="graph", kind="persistent", ignore_nulls=True)
    schema = E.schema(node_types=(NodeTypeSpec("n1", (FieldSpec("f", FieldType.FLOAT),)),), edge_types=(EdgeTypeSpec("e1", "n1", "n1", ()),), indexes=(sparse,))
    inst = build(1, CFG, E)[1]
    inst = dataclasses.replace(inst, schema=schema, nodes=[NodeRow("n1", 0, {"f": 1.5}), NodeRow("n1", 1, {})], edges=[], churn=[])
    lt = SelectQuery("n1", None, "join", ("f",), fn="<", arg="ids")
    assert select_known(lt, SelectSyntax(), Rewrites(), "graph", schema, inst) == "A43"
    assert select_known(lt, SelectSyntax(), Rewrites(), "rel", schema, inst) is None  # no index there
    assert select_known(dataclasses.replace(lt, fn="<="), SelectSyntax(), Rewrites(), "graph", schema, inst) is None
    full = dataclasses.replace(inst, nodes=[NodeRow("n1", 0, {"f": 1.5})])
    assert select_known(lt, SelectSyntax(), Rewrites(), "graph", schema, full) is None  # no null to lose
    # a sparse index on the base class covers the subclass (campaign a4n, seed 2008)
    based = E.schema(node_types=schema.node_types, edge_types=schema.edge_types, indexes=(dataclasses.replace(sparse, table="N0"),), base=NodeTypeSpec("N0", (FieldSpec("f", FieldType.FLOAT),)))
    assert select_known(lt, SelectSyntax(), Rewrites(), "graph", based, dataclasses.replace(inst, schema=based)) == "A43"
