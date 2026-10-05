"""The cov400 triage (triage_reports/cov400-2026-09-28.md): the new findings' models
and guards (O41 DISTINCT, O42 LIKE, O43 list IN, O44 depth-first TRAVERSE), the O5 /
O31 / O16 guard faces, and the two fuzzer bugs -- each against the probed engine
answer."""

import random

from ganda.orientdb.known_bugs import (
    map_value_index_known,
    o5_poly_sibling_index,
    o5_sibling_filter,
    o31_desc_walk,
    o41_values,
    o43_predicate,
    o44_dfs_reach,
)
from ganda.orientdb.java_hash import distinct_collides, java_hash, string_hash
from ganda.orientdb.model import EdgeRow, EdgeTypeSpec, FieldSpec, FieldType, IndexSpec, Instance, NodeRow, NodeTypeSpec, Schema
from ganda.orientdb.queries.traversal_ast import Hop, TraversalQuery
from ganda.orientdb.queries.predicates import Compound, Predicate, arithmetic
from ganda.orientdb.semantics import like_hit

# the 66 LONG values the live reducer kept: DISTINCT over them answers a duplicate,
# over any 65 of them it does not
DISTINCT_REPRO = [-290, None, 2**63 - 1, None, -(2**63), -364, -642, -122, -364, None, 794, 2**63 - 1, -874, -33, 492, -987, 592, 772, -(2**31), 484, 957, -903, 40, 2**53 + 1, 7, -482, -(2**62), -749, 2**53 + 1, None, -820, None, 568, None, 274, None, 703, 2**31, None, -693, 2**31 - 1, -51, -564, 1, 714, 2**63 - 1, -893, 304, -(2**31), None, -700, 603, -642, 939, 794, -33, None, 2**53, -745, 496, None, -894, 609, -291, -1, 0]


def _distinct_rows(values):
    seen = {}
    for v in values:
        seen.setdefault(repr(v), v)
    return [{"v": v} for v in seen.values()]


def test_java_hashes_match_the_jdk():
    # Long.hashCode, Integer, Boolean, String (UTF-16), the Aa/BB collision
    assert java_hash(0, FieldType.INT) == java_hash(-1, FieldType.INT) == java_hash(None, FieldType.INT) == 0
    assert java_hash(2**31, FieldType.INT) == java_hash(2**63 - 1, FieldType.INT) == -(2**31)
    assert java_hash(-5, FieldType.INT32) == -5 and java_hash(True, FieldType.BOOL) == 1231
    assert string_hash("v") == 118 and string_hash("Aa") == string_hash("BB") == 2112
    assert java_hash(1.0, FieldType.FLOAT) == 1072693248 and java_hash(1.0, FieldType.FLOAT32) == 1065353216
    # BigDecimal.valueOf(5.0) is 5.0 (unscaled 50, scale 1): 31 * 50 + 1
    assert java_hash(5.0, FieldType.DECIMAL) == 1551
    # java.util.Date: the epoch millis folded
    assert java_hash("1970-01-01 00:00:01", FieldType.DATETIME) == 1000


def test_o41_flags_the_minimized_repro_and_not_one_value_less():
    assert distinct_collides(_distinct_rows(DISTINCT_REPRO), {"v": FieldType.INT})
    assert not distinct_collides(_distinct_rows(DISTINCT_REPRO[:-1]), {"v": FieldType.INT})
    assert o41_values(DISTINCT_REPRO, "v", FieldType.INT)
    # the upstream repro: 64j and -64j-1 (j < 10) share hashes and one bin
    assert o41_values([v for j in range(10) for v in (64 * j, -64 * j - 1)], "v", FieldType.INT)
    rng = random.Random(7)
    for _ in range(100):
        vals = [rng.randint(-1000, 1000) for _ in range(200)] + [None, 0]
        assert not o41_values(vals, "v", FieldType.INT)


def test_o42_like_never_crosses_a_line_terminator():
    # probed on 3.2.56: every pattern below matches 'ab' and 'a\tb' only
    values = ["a\nb", "ab", "a\rb", "a\tb", "a b"]
    for pattern in ("a%", "%b", "%a%", "a%b", "A%"):
        assert [v for v in values if like_hit(v, pattern)] == ["ab", "a\tb"], pattern
    assert [v for v in values if like_hit(v, "a?b")] == ["a\tb"]
    assert like_hit("a.b", "a.b") and not like_hit("axb", "a.b") and like_hit("", "%")


def test_o43_list_in_on_a_doc_edge_attribute():
    schema = Schema((NodeTypeSpec("n1", ()),), (EdgeTypeSpec("e1", "n1", "n1", (FieldSpec("f", FieldType.LIST), FieldSpec("g", FieldType.INT))),))
    assert o43_predicate(schema, "e1", Predicate("edge", "f", "IN", "q", [[0]]))
    assert not o43_predicate(schema, "e1", Predicate("edge", "f", "=", "p", [0]))
    assert not o43_predicate(schema, "e1", Predicate("edge", "g", "IN", "q", [1, 2]))


def _inheritance_schema(indexes):
    base = NodeTypeSpec("N0", (FieldSpec("b1", FieldType.BOOL), FieldSpec("m", FieldType.MAP)))
    return Schema(
        (NodeTypeSpec("n1", (FieldSpec("f1", FieldType.DATE), FieldSpec("f2", FieldType.DATETIME))), NodeTypeSpec("n2", (FieldSpec("f1", FieldType.BOOL), FieldSpec("f2", FieldType.LIST)))),
        (),
        tuple(indexes),
        base=base,
    )


def test_o5_sibling_faces():
    between = Predicate("source", "f1", "BETWEEN", "p", ["1978-07-01", "2000-02-28"], ftype="date")
    tree = Compound("source", "AND", (Predicate("source", "b1", "=", "a", False, ftype="bool"), Compound("source", "OR", (Predicate("source", "b1", "=", "b", True, ftype="bool"), between))))
    flat = Compound("source", "AND", (Predicate("source", "b1", "=", "a", False, ftype="bool"), between))
    served = _inheritance_schema([IndexSpec("N0", "b1", kind="NOTUNIQUE_HASH_INDEX")])
    # probed: the OR form throws, the top-level BETWEEN answers, no base index answers
    assert o5_sibling_filter(served, "n1", tree, "doc")
    assert not o5_sibling_filter(served, "n1", flat, "doc")
    assert not o5_sibling_filter(_inheritance_schema([]), "n1", tree, "doc")
    # a sibling's LIST index on the same-named field converts a date pivot
    eq = Predicate("source", "f2", "=", "p", "2024-08-18 08:18:40", ftype="datetime")
    both = [IndexSpec("n1", "f2", data_model="graph"), IndexSpec("n2", "f2", data_model="graph")]
    assert o5_poly_sibling_index(_inheritance_schema(both), "n1", eq, "graph")
    assert not o5_poly_sibling_index(_inheritance_schema(both), "n1", eq, "doc")
    # probed: the sibling's index alone, or a range, answers
    assert not o5_poly_sibling_index(_inheritance_schema([IndexSpec("n2", "f2", data_model="graph")]), "n1", eq, "graph")
    assert not o5_poly_sibling_index(_inheritance_schema(both), "n1", Predicate("source", "f2", ">", "p", "2024-08-18 08:18:40", ftype="datetime"), "graph")
    # CONTAINSVALUE through a BY VALUE hash or dictionary map index: a 501
    cv = Predicate("source", "m", "CONTAINSVALUE", "p", 2**31, ftype="map")
    assert map_value_index_known(_inheritance_schema([IndexSpec("N0", "m", kind="NOTUNIQUE_HASH_INDEX", by="VALUE")]), "n1", cv, "doc") == "O5"
    assert map_value_index_known(_inheritance_schema([IndexSpec("N0", "m", kind="DICTIONARY", by="VALUE")]), "n1", cv, "doc") == "O5"
    assert map_value_index_known(_inheritance_schema([IndexSpec("N0", "m", kind="NOTUNIQUE", by="VALUE")]), "n1", cv, "doc") is None
    assert map_value_index_known(_inheritance_schema([IndexSpec("N0", "m", kind="NOTUNIQUE_HASH_INDEX", by="KEY")]), "n1", cv, "doc") is None


def test_o31_desc_walk():
    schema = Schema((NodeTypeSpec("n1", (FieldSpec("f", FieldType.INT),)),), (), (IndexSpec("n1", "f", ignore_nulls=False),))
    update = {"table": "n1", "write": "update", "field": "f", "tid": "n1/r1", "tv": 500, "remove": False}
    assert o31_desc_walk(schema, "n1", ["f"], ["DESC"], update, "doc")
    assert not o31_desc_walk(schema, "n1", ["f"], ["ASC"], update, "doc")
    assert not o31_desc_walk(schema, "n1", ["f"], ["DESC"], dict(update, tv=None), "doc")
    assert not o31_desc_walk(schema, "n1", ["f"], ["DESC"], None, "doc")
    hashed = Schema((NodeTypeSpec("n1", (FieldSpec("f", FieldType.INT),)),), (), (IndexSpec("n1", "f", kind="UNIQUE_HASH_INDEX"),))
    assert not o31_desc_walk(hashed, "n1", ["f"], ["DESC"], update, "doc")


def test_o44_depth_first_reach():
    schema = Schema((NodeTypeSpec("n1", ()),), (EdgeTypeSpec("e1", "n1", "n1", ()),))

    def inst(pairs):
        nodes = [NodeRow("n1", i, {}) for i in range(6)]
        return Instance(schema, nodes, [EdgeRow("e1", k, f"n1/r{a}", f"n1/r{b}", {}) for k, (a, b) in enumerate(pairs)])

    q = TraversalQuery("n1", (Hop("e1", "out"),), "reach3_count")
    # a chain: every record at one depth only
    assert not o44_dfs_reach(q, inst([(0, 1), (1, 2), (2, 3)]))
    # a shortcut from the start is marked at depth 1 before anything else (probed)
    assert not o44_dfs_reach(q, inst([(0, 1), (1, 2), (2, 3), (0, 3), (3, 4)]))
    # the probed miss: 0->b->x at distance 2, 0->a->y->x meets x at depth 3 first
    # (a expanded before b), and x's child z is never visited
    assert o44_dfs_reach(q, inst([(0, 2), (0, 1), (1, 3), (3, 4), (2, 4), (4, 5)]))


def test_the_fuzzer_bugs():
    from ganda.orientdb.queries import predicates

    # `VALUES CONTAINS` one past a stored Long.MAX stays a long
    assert predicates._offset(FieldType.INT, 2**63 - 1, 1) == 2**63 - 1

    class Pick:
        def __init__(self, pick):
            self.pick = pick

        def choice(self, xs):
            return self.pick

    p = Predicate("source", "f1", "=", "p", -0.0, ftype="float32")
    values = [0.0, 3.5, None]
    assert arithmetic(p, Pick(("-", 1)), "ar", FieldType.FLOAT32, values) is None
    assert arithmetic(p, Pick(("*", 2)), "ar", FieldType.FLOAT32, values) is not None
    assert arithmetic(Predicate("source", "f1", "=", "p", 0.0, ftype="float32"), Pick(("-", 1)), "ar", FieldType.FLOAT32, values) is not None
