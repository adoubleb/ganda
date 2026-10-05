from ganda.core.model import EdgeRow, EdgeTypeSpec, FieldSpec, FieldType, Instance, NodeRow, NodeTypeSpec, Schema
from ganda.core.oracle import build_result, fold_rows, normalize_multiset, normalize_set
from ganda.core.dedup import BugDeduplicator
from ganda.core.case import Comparison, QueryCase
from ganda.orientdb.semantics import agg, eq, orient_key, predicate_hit


from ganda.orientdb import PROVIDER, Schema  # noqa: E402,F811 -- bound to the OrientDB provider

def _case(comparison=Comparison.MULTISET_BY_ID, template="ir_traversal", params=None):
    return QueryCase(template, "e1", comparison, {"doc": "d", "graph": "g", "rel": "r", "graph:match": "m"}, params=params or {"reference": False}, shape="s", engine=PROVIDER)


def test_folding_and_flattening():
    rows = [{"id": "a", "v": ["x", ["y", None]]}, {"id": "a", "v": "z"}, {"id": "b", "v": None}]
    m, dups = fold_rows(rows, Comparison.MULTISET_BY_ID, "v")
    assert normalize_multiset(m["a"]) == ["x", "y", "z"] and normalize_multiset(m["b"]) == [] and not dups
    m, dups = fold_rows([{"id": "a", "v": 1}, {"id": "a", "v": 1}], Comparison.SCALAR_BY_ID, "v")
    assert dups and dups[0].record_id == "a"


def test_normalization_orders_like_orientdb():
    assert normalize_multiset(["￿", "\U0001F600", "a"], orient_key) == ["a", "\U0001F600", "￿"]
    assert normalize_set([1, 1, 2]) == [1, 2]
    assert normalize_multiset([0.0, -0.0]) == [-0.0, 0.0] and str(normalize_multiset([0.0, -0.0])[0]) == "-0.0"


def test_pairwise_mismatch_attribution():
    case = _case()
    rows = {"doc": [{"id": "a", "v": [1, 2]}], "graph": [{"id": "a", "v": [2, 1]}], "rel": [{"id": "a", "v": [1]}], "graph:match": [{"id": "a", "v": 1}, {"id": "a", "v": 2}]}
    r = build_result(case, rows, {s: "" for s in rows}, None)
    assert not r.ok
    assert r.failing_pairs == ["doc/rel", "graph/rel", "graph:match/rel"]


def test_error_classes():
    case = _case()
    r = build_result(case, {"doc": [], "graph": [], "rel": [], "graph:match": []}, {"doc": "boom", "graph": "", "rel": "", "graph:match": ""}, None)
    assert r.error_asymmetry and not r.symmetric_error and r.finding_class == "error_asymmetry"
    r = build_result(case, {s: [] for s in case.variants}, {s: "boom" for s in case.variants}, None)
    assert r.symmetric_error and r.ok


def test_reference_leg_sees_shared_corruption():
    schema = Schema((NodeTypeSpec("n1", ()),), (EdgeTypeSpec("e1", "n1", "n1", ()),))
    inst = Instance(schema, [NodeRow("n1", 0, {}), NodeRow("n1", 1, {})], [EdgeRow("e1", 0, "n1/r0", "n1/r1", {})])
    q = {"source_table": "n1", "source_predicate": None, "hops": [{"edge_type": "e1", "direction": "out", "predicate": None}], "projection": "ids", "field": None}
    case = _case(params={"traversal": q, "reference": True})
    rows = {s: [{"id": "n1/r0", "v": []}, {"id": "n1/r1", "v": []}] for s in case.variants}
    r = build_result(case, rows, {s: "" for s in rows}, inst)
    assert not r.ok and r.reference_mismatches and not r.mismatches and r.finding_class == "reference_mismatch"


def test_quarantine_keys_first_then_counts():
    case = _case()
    rows = {"doc": [{"id": "a", "v": [1]}], "graph": [{"id": "a", "v": [2]}], "rel": [{"id": "a", "v": [1]}], "graph:match": [{"id": "a", "v": [1]}]}
    r = build_result(case, rows, {s: "" for s in rows}, None)
    qn = BugDeduplicator()
    assert qn.admit(r) == (True, 1)
    assert qn.admit(r) == (False, 2)


def test_reference_semantics():
    assert predicate_hit(False, None, "<>", 1) and not predicate_hit(False, None, "<", 1) and predicate_hit(False, None, "IS NULL", None)
    assert not predicate_hit(True, None, "=", None) and predicate_hit(True, None, "IS DEFINED", None)
    assert not eq(-0.0, 0.0) and predicate_hit(True, -0.0, "<", 0.0)
    assert orient_key("\U0001F600") < orient_key("￿")
    assert predicate_hit(True, "abc", "LIKE", "a%") and predicate_hit(True, "abc", "LIKE", "%b%") and not predicate_hit(True, "abc", "LIKE", "b%")
    assert predicate_hit(True, [1, 2], "CONTAINS", 2) and predicate_hit(True, 5, "BETWEEN", [5, 6])
    assert agg("max", [None, 1, 3]) == 3 and agg("sum", [None]) == 0 and agg("max", [None]) is None and agg("distinct_count", [1, 1, 2]) == 2
