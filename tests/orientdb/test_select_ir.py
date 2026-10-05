import dataclasses
import re

from ganda.orientdb.generator import GeneratorConfig, build
from ganda.orientdb.guards import select_known
from ganda.orientdb.model import FieldSpec, FieldType, IndexSpec, Instance, NodeRow, NodeTypeSpec, Schema
from ganda.orientdb.queries import predicates
from ganda.orientdb.queries.base import Comparison, QueryCase
from ganda.orientdb.queries.predicates import Predicate
from ganda.orientdb.queries.select_generator import build_twins
from ganda.orientdb.queries.select_ir import COVERAGE_DENOMINATOR, SelectQuery, all_coverage_signatures, coverage_signature, evaluate, from_params, records
from ganda.orientdb.queries.select_spellings import BASE, Spelling, Twins, applicable, render
from ganda.orientdb.queries.templates import all_cases, reference_for

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


def test_aggregates_over_no_rows_answer_no_row_but_count():
    schema, inst = _fixture()
    none = _gt("f1", 100)
    for fn in ("max", "min", "sum", "avg", "median", "distinct_count", "set", "list"):
        assert evaluate(SelectQuery("n1", none, "agg", ("f1",), fn=fn), inst) == []
    assert evaluate(SelectQuery("n1", none, "agg", ("f1",), fn="count"), inst) == [{"v": 0}]
    assert evaluate(SelectQuery("n1", none, "count"), inst) == [{"v": 0}]
    # rows that are all null: max is a null row, sum 0
    nulls = Predicate("source", "f1", "IS NULL", "", None)
    assert evaluate(SelectQuery("n1", nulls, "agg", ("f1",), fn="max"), inst) == [{"v": None}]
    assert evaluate(SelectQuery("n1", nulls, "agg", ("f1",), fn="sum"), inst) == [{"v": 0}]


def test_filtered_shapes():
    schema, inst = _fixture()
    p = _gt("f3", 1)  # r1..r4
    assert evaluate(SelectQuery("n1", p, "ids"), inst) == [{"id": f"n1/r{i}"} for i in (1, 2, 3, 4)]
    assert evaluate(SelectQuery("n1", p, "agg", ("f1",), fn="max"), inst) == [{"v": 7}]
    assert sorted(evaluate(SelectQuery("n1", p, "group", ("f2",), fn="count_star"), inst), key=repr) == sorted([{"k": "B", "v": 1}, {"k": "b", "v": 1}, {"k": None, "v": 1}, {"k": "a", "v": 1}], key=repr)
    assert evaluate(SelectQuery("n1", p, "group", ("f2",), fn="sum", arg="f1"), inst) == [{"k": "B", "v": 1}, {"k": "b", "v": 0}, {"k": None, "v": 3}, {"k": "a", "v": 7}]
    # nulls sort first descending; the id tiebreak stays ascending
    assert evaluate(SelectQuery("n1", p, "page", ("f1",), directions=("DESC",), skip=0, limit=4), inst) == [{"id": "n1/r2"}, {"id": "n1/r4"}, {"id": "n1/r3"}, {"id": "n1/r1"}]
    assert evaluate(SelectQuery("n1", p, "keys", ("f1",), directions=("ASC",), skip=1), inst) == [{"a": 3}, {"a": 7}, {"a": None}]
    assert evaluate(SelectQuery("n1", p, "method", ("f1",), fn="{f} + 1"), inst) == {"n1/r1": 2, "n1/r3": 4, "n1/r4": 8}


def test_spelling_tags_and_applicability():
    schema, inst = _fixture()
    q = SelectQuery("n1", _gt("f1", 2), "agg", ("f1",), fn="max")
    twins = build_twins(q, __import__("random").Random(1), schema, inst)
    assert Spelling("rw", "nest", "order").tag == "order+rw+nest"
    assert applicable(q, schema, Spelling(form="order"), twins, "doc")
    assert not applicable(q, schema, Spelling(position="poly"), twins, "doc")  # no base class
    assert not applicable(q, schema, Spelling(form="set"), twins, "doc")
    assert not applicable(dataclasses.replace(q, where=None), schema, Spelling(position="nest"), twins, "doc")
    assert render(q, schema, Spelling(position="nest", form="order"), twins, "doc") == "SELECT f1 AS v FROM (SELECT FROM n1 WHERE (f1 > :p) AND f1 IS NOT NULL) ORDER BY f1 DESC LIMIT 1"
    assert render(q, schema, Spelling(position="scan"), twins, "doc") == "SELECT max(f1) AS v FROM (SELECT FROM n1) WHERE f1 > :p"
    empty = SelectQuery("n1", _gt("f1", 100), "agg", ("f1",), fn="max")
    assert not applicable(empty, schema, Spelling(form="order"), build_twins(empty, __import__("random").Random(1), schema, inst), "doc")


def test_order_walk_guards_hold_on_the_class_only():
    ix = IndexSpec("n1", "f1", side="graph", kind="NOTUNIQUE", ignore_nulls=True)
    schema, inst = _fixture([ix])
    q = SelectQuery("n1", None, "agg", ("f1",), fn="max")
    twins = Twins(present=[3, 1, 3, 7])
    assert select_known(q, Spelling(form="order"), twins, "graph", schema, inst) == "O18"
    assert select_known(q, Spelling(form="order"), twins, "doc", schema, inst) is None
    assert select_known(q, Spelling(form="order", position="scan"), twins, "graph", schema, inst) is None
    keys = SelectQuery("n1", _gt("f3", 0), "keys", ("f1",), directions=("DESC",))
    assert select_known(keys, BASE, Twins(), "graph", schema, inst) == "O18"
    assert select_known(keys, Spelling(position="nest"), Twins(), "graph", schema, inst) is None


def test_scan_target_keeps_only_the_scan_side_findings():
    # a truncation-sensitive double pivot on an indexed INT column is O16 through the
    # index; the subquery target scans it
    ix = IndexSpec("n1", "f1", side="rel", kind="NOTUNIQUE")
    schema, inst = _fixture([ix])
    p = Predicate("source", "f1", "=", "p", 3.5, ftype="int")
    q = SelectQuery("n1", p, "ids")
    twins = Twins()
    assert select_known(q, BASE, twins, "rel", schema, inst) == "O16"
    assert select_known(q, Spelling(position="nest"), twins, "rel", schema, inst) == "O16"
    assert select_known(q, Spelling(position="scan"), twins, "rel", schema, inst) is None
    assert select_known(q, BASE, twins, "doc", schema, inst) is None


def test_distinct_count_and_avg_findings():
    schema, inst = _fixture()
    q = SelectQuery("n1", None, "agg", ("f1",), fn="distinct_count")
    assert select_known(q, Spelling(form="fn"), Twins(present=[3, 1]), "doc", schema, inst) == "O33"
    assert select_known(q, Spelling(form="set"), Twins(present=[3, 1]), "doc", schema, inst) is None
    avg = SelectQuery("n1", None, "agg", ("f3",), fn="avg")
    assert select_known(avg, BASE, Twins(present=[1, 2]), "doc", schema, inst) == "O28"
    assert select_known(avg, BASE, Twins(present=[1, 3]), "doc", schema, inst) is None


def _cases(seeds=range(1000, 1012)):
    for seed in seeds:
        schema, inst, rng = build(seed, CFG)
        yield schema, inst, all_cases(rng, schema, inst, ir_cases=0, select_cases=30)


_BIND = re.compile(r"(?<![\w:]):([A-Za-z_]\w*)")


def _read_of(text: str) -> str:
    """A transaction script's read (the write differs per side: `CREATE VERTEX`)."""
    m = re.match(r"BEGIN; .*?; LET \$r = \((.*) TIMEOUT \d+ EXCEPTION\); ROLLBACK; RETURN \$r$", text)
    return m.group(1) if m else text


def test_every_case_is_well_formed():
    seen_kinds = set()
    for schema, inst, cases in _cases():
        for c in cases:
            assert c.template_name == "ir_select"
            q = from_params(c.params["select"])
            texts = {side: _read_of(c.queries[side]) for side in ("doc", "graph", "rel") if side in c.queries}
            if q.edge:
                # the doc side reads the embedded link documents, and only ids / count
                assert texts["graph"] == texts["rel"]
                assert ("doc" in texts) == (q.kind in ("ids", "count")) == (not c.params.get("partial"))
            else:
                assert texts["doc"] == texts["graph"] == texts["rel"]
            if q.tx:
                assert all(t.startswith("BEGIN; ") and t.endswith("ROLLBACK; RETURN $r") for t in c.queries.values())
            for side, text in c.queries.items():
                for name in _BIND.findall(text):
                    assert name in c.bind_vars, (side, text, name)
                if ":" in side and not q.edge:
                    base, tag = side.split(":", 1)
                    # every variant is asked on every side it applies to, the door aside
                    if tag != "index":
                        assert all(f"{s}:{tag}" in c.queries for s in ("doc", "graph", "rel")), side
            assert coverage_signature(q) in all_coverage_signatures()
            seen_kinds.add(q.kind)
            has_ref, expected = reference_for(c, inst)
            assert has_ref == (c.params["reference"] is not False)
            if not has_ref:
                assert q.kind == "page"
                expected = evaluate(q, inst)
            if c.comparison in (Comparison.SCALAR_BY_ID, Comparison.MULTISET_BY_ID):
                assert isinstance(expected, dict)
            else:
                assert isinstance(expected, list)
            # a twin filter is the same predicate over every record
            for name in ("rw", "rev", "arith"):
                if name in c.params:
                    twin = Predicate.from_dict(c.params[name])
                    for n in records(q, inst):
                        assert predicates.evaluate(n.values, twin) == predicates.evaluate(n.values, q.where), (name, c.params[name])
    assert seen_kinds == {"ids", "count", "field", "method", "agg", "group", "distinct", "page", "keys", "join"}
    assert COVERAGE_DENOMINATOR == len(all_coverage_signatures())


def test_supply_is_deterministic_and_prefix_stable():
    schema, inst, rng = build(1003, CFG)
    a = all_cases(rng, schema, inst, ir_cases=0, select_cases=30)
    schema, inst, rng = build(1003, CFG)
    b = all_cases(rng, schema, inst, ir_cases=0, select_cases=10)
    assert [c.queries for c in a[:10]] == [c.queries for c in b]
    # adding the select supply moves no other case
    schema, inst, rng = build(1003, CFG)
    before = all_cases(rng, schema, inst, ir_cases=10, select_cases=0)
    schema, inst, rng = build(1003, CFG)
    after = all_cases(rng, schema, inst, ir_cases=10, select_cases=30)
    assert [c.queries for c in after[: len(before)]] == [c.queries for c in before]


def test_index_door_is_withheld_where_a_composite_ignoring_nulls_lacks_a_match():
    from ganda.orientdb.queries.select_support import door_complete

    ix = IndexSpec("n1", "f3", side="both", kind="NOTUNIQUE", extra_fields=("f1",), ignore_nulls=True)
    schema, inst = _fixture([ix])
    # f3 = 3 is r2, whose f1 is absent: the composite has no entry for it
    miss = Predicate("source", "f3", "=", "p", 3, ftype="int")
    hit = Predicate("source", "f3", "=", "p", 1, ftype="int")
    assert not door_complete(ix, "n1", miss, inst)
    assert door_complete(ix, "n1", hit, inst)
    assert door_complete(dataclasses.replace(ix, ignore_nulls=False), "n1", miss, inst)
    import random

    assert build_twins(SelectQuery("n1", miss, "ids"), random.Random(1), schema, inst).door == {}
    assert set(build_twins(SelectQuery("n1", hit, "ids"), random.Random(1), schema, inst).door) == {"doc", "graph", "rel"}
