import re

from ganda.orientdb.facade.generate import GeneratorConfig, build
from ganda.orientdb.known_bugs.guards import select_known
from ganda.core.data.model import FieldSpec, FieldType, IndexSpec, Instance, NodeRow, NodeTypeSpec, Schema
from ganda.core.oracle.case import QueryCase
from ganda.orientdb.facade.predicates import Predicate
from ganda.orientdb.concrete_syntax.select import build_rewrites
from ganda.core.query.ir.select import COVERAGE_DENOMINATOR, SelectQuery, all_coverage_signatures, coverage_signature, from_params
from ganda.orientdb.concrete_syntax.select import BASE, SelectSyntax, Rewrites, applicable, render
from ganda.core.query.generators.registry import all_cases

from ganda.orientdb import Schema  # noqa: E402,F811 -- bound to the OrientDB provider

CFG = GeneratorConfig(inheritance_probability=0.5, narrow_type_probability=0.3, date_map_probability=0.5, collate_probability=0.4, list_index_probability=0.5, custom_key_probability=0.3)


def _fixture(indexes=()):
    schema = Schema(
        node_types=(NodeTypeSpec("n1", (FieldSpec("f1", FieldType.INT), FieldSpec("f2", FieldType.STRING), FieldSpec("f3", FieldType.INT))),),
        edge_types=(),
        indexes=tuple(indexes),
    )
    nodes = [
        NodeRow("n1", 0, {"f1": 3, "f2": "a", "f3": 1}),
        NodeRow("n1", 1, {"f1": 1, "f2": "B", "f3": 2}),
        NodeRow("n1", 2, {"f2": "b", "f3": 3}),
        NodeRow("n1", 3, {"f1": 3, "f3": 4}),
        NodeRow("n1", 4, {"f1": 7, "f2": "a", "f3": 6}),
    ]
    return schema, Instance(schema, nodes, [])


def _gt(field, v):
    return Predicate("source", field, ">", "p", v, ftype="int")


def test_syntax_tags_and_applicability():
    schema, inst = _fixture()
    q = SelectQuery("n1", _gt("f1", 2), "agg", ("f1",), fn="max")
    rewrites = build_rewrites(q, __import__("random").Random(1), schema, inst)
    assert SelectSyntax("poly", "order").tag == "order+poly"
    assert applicable(q, schema, SelectSyntax(form="order"), rewrites, "doc")
    assert not applicable(q, schema, SelectSyntax(position="poly"), rewrites, "doc")  # no base class
    assert not applicable(q, schema, SelectSyntax(form="set"), rewrites, "doc")
    assert render(q, schema, SelectSyntax(form="order"), rewrites, "doc") == "SELECT f1 AS v FROM n1 WHERE (f1 > :p) AND f1 IS NOT NULL ORDER BY f1 DESC LIMIT 1"
    empty = SelectQuery("n1", _gt("f1", 100), "agg", ("f1",), fn="max")
    assert not applicable(empty, schema, SelectSyntax(form="order"), build_rewrites(empty, __import__("random").Random(1), schema, inst), "doc")


def test_order_walk_guards_hold_on_the_class_only():
    # a composite index ignoring nulls skips n1/3 (f1 present, f2 absent), so the order
    # form's `max` misses it
    ix = IndexSpec("n1", "f1", extra_fields=("f2",), data_model="graph", kind="NOTUNIQUE", ignore_nulls=True)
    schema, inst = _fixture([ix])
    q = SelectQuery("n1", None, "agg", ("f1",), fn="max")
    rewrites = Rewrites(present=[3, 1, 3, 7])
    assert select_known(q, SelectSyntax(form="order"), rewrites, "graph", schema, inst) == "O18"
    assert select_known(q, SelectSyntax(form="order"), rewrites, "doc", schema, inst) is None
    assert select_known(q, SelectSyntax(form="order", position="scan"), rewrites, "graph", schema, inst) is None
    keys = SelectQuery("n1", _gt("f3", 0), "keys", ("f1",), directions=("DESC",))
    assert select_known(keys, BASE, Rewrites(), "graph", schema, inst) == "O18"
    assert select_known(keys, SelectSyntax(position="nest"), Rewrites(), "graph", schema, inst) is None


def test_o18_needs_a_null_key_on_3_2_57():
    # a single-field index ignoring nulls: the walk lacks only the null records, which
    # the order form's `IS NOT NULL` excludes; the keys kind lacks n1/2 (no f1)
    schema, inst = _fixture([IndexSpec("n1", "f1", data_model="graph", kind="NOTUNIQUE", ignore_nulls=True)])
    q = SelectQuery("n1", None, "agg", ("f1",), fn="max")
    assert select_known(q, SelectSyntax(form="order"), Rewrites(present=[3, 1, 3, 7]), "graph", schema, inst) is None
    keys = SelectQuery("n1", _gt("f3", 0), "keys", ("f1",), directions=("DESC",))
    assert select_known(keys, BASE, Rewrites(), "graph", schema, inst) == "O18"
    full = Instance(schema, [n for n in inst.nodes if n.values.get("f1") is not None], [])
    assert select_known(keys, BASE, Rewrites(), "graph", schema, full) is None



def test_distinct_count_and_avg_are_not_withheld():
    # O33 (count(distinct())) and O28 (avg over integers) answer alike on every data
    # model, so they are compared across data models like any other aggregate
    schema, inst = _fixture()
    q = SelectQuery("n1", None, "agg", ("f1",), fn="distinct_count")
    assert select_known(q, SelectSyntax(form="fn"), Rewrites(present=[3, 1]), "doc", schema, inst) is None
    avg = SelectQuery("n1", None, "agg", ("f3",), fn="avg")
    assert select_known(avg, BASE, Rewrites(present=[1, 2]), "doc", schema, inst) is None


def _cases(seeds=range(1000, 1012)):
    for seed in seeds:
        schema, inst, rng = build(seed, CFG)
        yield schema, inst, all_cases(rng, schema, inst, traversal_cases=0, select_cases=30)


_BIND = re.compile(r"(?<![\w:]):([A-Za-z_]\w*)")


def _read_of(text: str) -> str:
    """A transaction script's read (the write differs per data model: `CREATE VERTEX`)."""
    m = re.match(r"BEGIN; .*?; LET \$r = \((.*) TIMEOUT \d+ EXCEPTION\); ROLLBACK; RETURN \$r$", text)
    return m.group(1) if m else text


def test_every_case_is_well_formed():
    seen_kinds = set()
    for schema, inst, cases in _cases():
        for c in cases:
            assert c.generator_name == "ir_select"
            q = from_params(c.params["select"])
            texts = {data_model: _read_of(c.queries[data_model]) for data_model in ("doc", "graph", "rel") if data_model in c.queries}
            if q.edge:
                # the doc data model reads the embedded link documents, and only ids / count
                assert texts["graph"] == texts["rel"]
                assert ("doc" in texts) == (q.kind in ("ids", "count")) == (not c.params.get("partial"))
            else:
                assert texts["doc"] == texts["graph"] == texts["rel"]
            if q.tx:
                assert all(t.startswith("BEGIN; ") and t.endswith("ROLLBACK; RETURN $r") for t in c.queries.values())
            for data_model, text in c.queries.items():
                for name in _BIND.findall(text):
                    assert name in c.bind_vars, (data_model, text, name)
                if ":" in data_model and not q.edge:
                    base, tag = data_model.split(":", 1)
                    # every variant is asked on every data model it applies to, the door aside
                    if tag != "index":
                        assert all(f"{s}:{tag}" in c.queries for s in ("doc", "graph", "rel")), data_model
            assert coverage_signature(q) in all_coverage_signatures()
            seen_kinds.add(q.kind)
    assert seen_kinds == {"ids", "count", "field", "method", "agg", "group", "distinct", "page", "keys", "join"}
    assert COVERAGE_DENOMINATOR == len(all_coverage_signatures())


def test_supply_is_deterministic_and_prefix_stable():
    schema, inst, rng = build(1003, CFG)
    a = all_cases(rng, schema, inst, traversal_cases=0, select_cases=30)
    schema, inst, rng = build(1003, CFG)
    b = all_cases(rng, schema, inst, traversal_cases=0, select_cases=10)
    assert [c.queries for c in a[:10]] == [c.queries for c in b]
    # adding the select supply moves no other case
    schema, inst, rng = build(1003, CFG)
    before = all_cases(rng, schema, inst, traversal_cases=10, select_cases=0)
    schema, inst, rng = build(1003, CFG)
    after = all_cases(rng, schema, inst, traversal_cases=10, select_cases=30)
    assert [c.queries for c in after[: len(before)]] == [c.queries for c in before]

