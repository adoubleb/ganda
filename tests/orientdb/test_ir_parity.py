"""The select and path IRs' reach: the semi-join leaf, the `join` kind,
edge-type targets, the transaction door, the stratified deck, the path IR and the
index-level mutants."""
import random
from collections import Counter

from ganda.orientdb.data_generator import GeneratorConfig, build
from ganda.orientdb.known_bugs import select_known
from ganda.core.model import EdgeRow, EdgeTypeSpec, FieldSpec, FieldType, IndexSpec, Instance, NodeRow, NodeTypeSpec, Schema
from ganda.orientdb.mutants import MUTANTS_BY_NAME
from ganda.orientdb.queries import predicates
from ganda.core.case import Comparison, QueryCase
from ganda.orientdb.queries.path_ast import PathQuery, evaluate as evaluate_path, render_all
from ganda.orientdb.queries.predicate_syntax import render_predicate as render
from ganda.orientdb.queries.predicates import Compound, Predicate
from ganda.orientdb.queries.select_generator import KIND_WEIGHTS, build_case, strata
from ganda.orientdb.queries.select_ast import KINDS, SelectQuery, evaluate, tx_instance
from ganda.orientdb.queries.select_syntax import BASE, SelectSyntax, Rewrites, applicable, render as render_select
from ganda.orientdb.queries.select_support import tx_write
from ganda.orientdb.queries.registry import all_cases

from ganda.orientdb import Schema  # noqa: E402,F811 -- bound to the OrientDB provider

CFG = GeneratorConfig(inheritance_probability=0.5, narrow_type_probability=0.3, date_map_probability=0.5, collate_probability=0.4, list_index_probability=0.5, custom_key_probability=0.3)


def _fixture(indexes=(), edge_indexes=()):
    schema = Schema(
        node_types=(
            NodeTypeSpec("n1", (FieldSpec("f1", FieldType.INT), FieldSpec("f2", FieldType.STRING))),
            NodeTypeSpec("n2", (FieldSpec("g1", FieldType.INT),)),
        ),
        edge_types=(EdgeTypeSpec("e1", "n1", "n2", (FieldSpec("w", FieldType.INT),)),),
        indexes=tuple(indexes) + tuple(edge_indexes),
    )
    nodes = [
        NodeRow("n1", 0, {"f1": 1, "f2": "a"}),
        NodeRow("n1", 1, {"f1": 2, "f2": "b"}),
        NodeRow("n1", 2, {"f1": 2}),
        NodeRow("n1", 3, {"f2": "c"}),
        NodeRow("n2", 0, {"g1": 2}),
        NodeRow("n2", 1, {"g1": 7}),
        NodeRow("n2", 2, {}),
    ]
    edges = [
        EdgeRow("e1", 0, "n1/r0", "n2/r0", {"w": 1}),
        EdgeRow("e1", 1, "n1/r0", "n2/r1", {"w": 5}),
        EdgeRow("e1", 2, "n1/r1", "n2/r1", {}),
    ]
    return schema, Instance(schema, nodes, edges)


def _insub(q=None, scan=False, pivot=None):
    return Predicate("source", "f1", "INSUB", "", pivot if pivot is not None else [], True, "int", False, sub=("n2", "g1", q, scan))


# --- the semi-join leaf -----------------------------------------------------------------------


def test_insub_renders_evaluates_and_round_trips():
    schema, inst = _fixture()
    q = Predicate("source", "g1", ">", "ps", 1, ftype="int")
    p = predicates.resolve_subs(_insub(q), inst)
    assert p.pivot == [2, 7]
    assert render(p) == "f1 IN (SELECT g1 FROM n2 WHERE g1 > :ps)"
    assert p.bind_vars == {"ps": 1}
    assert [n.record_id for n in inst.nodes_of("n1") if predicates.evaluate(n.values, p)] == ["n1/r1", "n1/r2"]
    assert Predicate.from_dict(p.to_dict()) == p
    # the rewrite reads the subquery over a subquery target; the literal concrete syntax has none
    rw = predicates.rewrite(p, random.Random(1), "q")
    assert rw.sub[3] and render(rw) == "f1 IN (SELECT g1 FROM (SELECT FROM n2) WHERE g1 > :ps)"
    assert predicates.reverse(p) is None and predicates.rewrite(rw, random.Random(1), "q") is None
    # an unfiltered subquery answers a null for a record without the field
    whole = predicates.resolve_subs(_insub(), inst)
    assert whole.pivot == [2, 7, None] and whole.bind_vars == {}


def test_insub_guards():
    ix = IndexSpec("n1", "f1", data_model="graph")
    schema, inst = _fixture([ix])
    null_listed = predicates.resolve_subs(_insub(), inst)  # [2, 7, None]
    q = SelectQuery("n1", null_listed, "ids")
    # O14 where the outer lookup is served by an index on f1 -- not over a subquery target
    assert select_known(q, BASE, Rewrites(), "graph", schema, inst) == "O14"
    assert select_known(q, SelectSyntax(position="scan"), Rewrites(), "graph", schema, inst) is None
    assert select_known(q, BASE, Rewrites(), "doc", schema, inst) is None
    # O39: a CONTAINSVALUE in the inner predicate, on every data model and position
    cv = Predicate("source", "g1", "CONTAINSVALUE", "ps", 2**40, ftype="map")
    q39 = SelectQuery("n1", _insub(cv, pivot=[]), "ids")
    assert {select_known(q39, sp, Rewrites(), s, schema, inst) for s in ("doc", "rel") for sp in (BASE, SelectSyntax(position="scan"))} == {"O39"}


# --- the join kind ----------------------------------------------------------------------------


def test_join_reference_keeps_the_outer_records_only():
    schema, inst = _fixture()
    # a filter keeps the outer records only; the LET still reads the whole class
    kept = SelectQuery("n1", Predicate("source", "f2", "IS NOT NULL", "", None), "join", ("f1",), fn="<=", arg="count")
    assert evaluate(kept, inst) == {"n1/r0": 1, "n1/r1": 3, "n1/r3": 1}


def test_join_spellings_and_o1():
    ix = IndexSpec("n1", "f2", data_model="rel")
    schema, inst = _fixture([ix])
    where = Predicate("source", "f2", "=", "p", "a", ftype="string")
    q = SelectQuery("n1", where, "join", ("f1",), fn="<", arg="ids")
    t = Rewrites()
    assert render_select(q, schema, BASE, t, "doc") == "SELECT id, $m.id AS v FROM n1 LET $m = (SELECT id FROM n1 WHERE f1 < $parent.$current.f1) WHERE f2 = :p"
    assert render_select(q, schema, SelectSyntax(form="jrev", position="nest"), t, "doc") == "SELECT id, $m.id AS v FROM (SELECT FROM n1 WHERE f2 = :p) LET $m = (SELECT id FROM n1 WHERE $parent.$current.f1 > f1)"
    assert render_select(q, schema, SelectSyntax(form="jscan", position="scan"), t, "doc") == "SELECT id, $m.id AS v FROM (SELECT FROM n1) LET $m = (SELECT id FROM (SELECT FROM n1) WHERE f1 < $parent.$current.f1) WHERE f2 = :p"
    assert not applicable(SelectQuery("n1", None, "ids"), schema, SelectSyntax(form="jscan"), t, "doc")
    # O1: the correlated LET under a filter an index of the data model serves -- not over a subquery target
    assert select_known(q, BASE, t, "rel", schema, inst) == "O1"
    assert select_known(q, SelectSyntax(position="nest"), t, "rel", schema, inst) is None
    assert select_known(q, BASE, t, "doc", schema, inst) is None


# --- edge-type targets ------------------------------------------------------------------------


def test_edge_target_reference_and_doc_spelling():
    schema, inst = _fixture()
    p = Predicate("edge", "w", ">", "p", 2, ftype="int")
    q = SelectQuery("e1", p, "ids", edge=True)
    assert evaluate(q, inst) == [{"id": "e1/e1"}]
    t = Rewrites(rw=predicates.rewrite(p, random.Random(1), "q"))
    assert render_select(q, schema, BASE, t, "graph") == "SELECT eid AS id FROM e1 WHERE w > :p"
    assert render_select(q, schema, BASE, t, "doc") == "SELECT v AS id FROM (SELECT v FROM (SELECT e1[w > :p].eid AS v FROM n1 UNWIND v) WHERE v IS NOT NULL)"
    assert applicable(q, schema, SelectSyntax(filter="rw"), t, "doc") and not applicable(q, schema, SelectSyntax(position="scan"), t, "doc")
    assert applicable(q, schema, SelectSyntax(position="scan"), t, "graph")
    page = SelectQuery("e1", None, "page", ("w",), directions=("DESC",), limit=2, edge=True)
    assert not applicable(page, schema, BASE, t, "doc")
    assert render_select(page, schema, BASE, t, "rel") == "SELECT eid AS id FROM e1 ORDER BY w DESC, eid ASC SKIP 0 LIMIT 2"
    c = build_case(page, random.Random(1), schema, inst)
    assert c.params["partial"] and "doc" not in c.queries and {"graph", "rel"} <= set(c.queries)


# --- the transaction door ---------------------------------------------------------------------


def test_tx_view_render_and_o31():
    ix = IndexSpec("n1", "f1", data_model="graph", kind="NOTUNIQUE")
    schema, inst = _fixture([ix])
    tx = {"table": "n1", "write": "insert", "field": "f1", "tid": "n1/tx0", "tv": 5, "remove": False}
    assert [n.record_id for n in tx_instance(inst, tx).nodes_of("n1")][-1] == "n1/tx0"
    where = Predicate("source", "f1", ">", "p", 1, ftype="int")
    q = SelectQuery("n1", where, "count", tx=tx)
    assert evaluate(q, inst) == [{"v": 3}]
    text = render_select(q, schema, BASE, Rewrites(), "graph")
    assert text == "BEGIN; CREATE VERTEX n1 SET id = :tid, f1 = :tv; LET $r = (SELECT count(*) AS v FROM n1 WHERE f1 > :p TIMEOUT 20000 EXCEPTION); ROLLBACK; RETURN $r"
    assert tx_write(tx, "rel").startswith("INSERT INTO n1") and tx_write(dict(tx, write="delete"), "graph") == "DELETE VERTEX n1 WHERE id = :tid"
    # O31 through the graph data model's SB-tree index; a subquery target and the other sides are right
    assert select_known(q, BASE, Rewrites(), "graph", schema, inst) == "O31"
    assert select_known(q, SelectSyntax(position="scan"), Rewrites(), "graph", schema, inst) is None
    assert select_known(q, BASE, Rewrites(), "doc", schema, inst) is None


# --- the deck -----------------------------------------------------------------------------------


def test_deck_weights_and_supply():
    schema, inst = _fixture()
    deck = Counter(strata(schema, inst))
    assert deck[("n1", False, "ids")] == KIND_WEIGHTS["ids"] and deck[("e1", True, "ids")] == 1
    assert ("e1", True, "join") not in deck and {k for (_t, _e, k) in deck} == set(KINDS)
    kinds, edge, tx, insub = Counter(), 0, 0, 0
    for seed in range(1000, 1008):
        schema, inst, rng = build(seed, CFG)
        for c in all_cases(rng, schema, inst, traversal_cases=0, select_cases=30):
            s = c.params["select"]
            kinds[s["kind"]] += 1
            edge += s["edge"]
            tx += bool(s["tx"])
            insub += bool(predicates.subqueries(Predicate.from_dict(s["where"])))
    assert set(kinds) == set(KINDS) and edge and tx and insub


def test_walk_tier_orders_every_indexed_class_both_ways():
    ix = IndexSpec("n1", "f1", data_model="doc", kind="NOTUNIQUE")
    schema, inst = _fixture([ix])
    from ganda.orientdb.queries.select_generator import generate_select_cases

    cases = generate_select_cases(random.Random(3), schema, inst, per_instance=6)
    walks = [c.params["select"] for c in cases if c.params["select"]["kind"] == "keys" and c.params["select"]["where"] is None]
    assert sorted(w["directions"][0] for w in walks if w["table"] == "n1" and w["fields"][0] == "f1") == ["ASC", "DESC"]


def test_tier_lists_every_edge_type_whole_and_filtered():
    # the tier is six questions here: n1, n2, e1 twice, n1's two walks
    ix = IndexSpec("n1", "f1", data_model="doc", kind="NOTUNIQUE")
    schema, inst = _fixture([ix])
    from ganda.orientdb.queries.select_generator import generate_select_cases

    for seed in range(8):
        cases = generate_select_cases(random.Random(seed), schema, inst, per_instance=6)
        edges = [c.params["select"] for c in cases if c.params["select"]["edge"]]
        assert sorted(e["where"] is not None for e in edges) == [False, True] and {e["kind"] for e in edges} == {"ids"}


# --- the path IR ------------------------------------------------------------------------------


def _chain():
    schema = Schema(node_types=(NodeTypeSpec("n1", ()),), edge_types=(EdgeTypeSpec("e1", "n1", "n1", ()),))
    nodes = [NodeRow("n1", i, {}) for i in range(5)]
    edges = [EdgeRow("e1", i, f"n1/r{a}", f"n1/r{b}", {}) for i, (a, b) in enumerate([(0, 1), (1, 2), (2, 3), (1, 1)])]
    return schema, Instance(schema, nodes, edges)


def test_path_ir_answers_and_spellings():
    schema, inst = _chain()
    q = PathQuery("n1/r0", "n1", "n1/r3", "n1", "OUT", "e1", 3)
    assert evaluate_path(q, inst) == [{"v": 4}]
    queries, known_bugs = render_all(q, schema, inst)
    assert {"doc", "graph", "rel", "rel:inline", "rel:asc", "doc:deep", "graph:unbounded"} <= set(queries)
    assert known_bugs == {"graph": "O35", "rel:asc": "O40"}
    assert "TRAVERSE e1, target FROM (SELECT FROM n1 WHERE id = :a) MAXDEPTH 3 STRATEGY BREADTH_FIRST" in queries["doc"]
    assert queries["rel:inline"].index("SELECT 3 AS d") < queries["rel:inline"].index("SELECT 0 AS d")
    # beyond the bound: no row, and no unbounded form
    near = PathQuery("n1/r0", "n1", "n1/r3", "n1", "OUT", "e1", 2)
    assert evaluate_path(near, inst) == [] and "graph:unbounded" not in render_all(near, schema, inst)[0]
    # IN: no doc data model; BOTH or every type: no rel data model
    assert "doc" not in render_all(PathQuery("n1/r3", "n1", "n1/r0", "n1", "IN", "e1", 3), schema, inst)[0]
    assert "rel" not in render_all(PathQuery("n1/r0", "n1", "n1/r3", "n1", "BOTH", "e1", 3), schema, inst)[0]
    assert "rel" not in render_all(PathQuery("n1/r0", "n1", "n1/r3", "n1", "OUT", None, 3), schema, inst)[0]


# --- the index-level mutants ------------------------------------------------------------------


def test_index_mutants_touch_only_servable_spellings():
    ix = IndexSpec("n1", "f1", data_model="both", kind="NOTUNIQUE")
    schema, _inst = _fixture([ix])
    case = QueryCase("ir_select", "", Comparison.NESTED_VALUE, {
        "doc": "SELECT id FROM n1 WHERE f1 > :p ORDER BY f1 DESC",
        "graph": "SELECT id FROM n1 WHERE f1 > :p",
        "graph:scan": "SELECT id FROM (SELECT FROM n1) WHERE f1 > :p",
        "graph:rev": "SELECT id FROM n1 WHERE :p < f1",
        "rel": "SELECT id FROM n1 WHERE f1 = :p",
    }, {}, {"table": "n1"})
    g = MUTANTS_BY_NAME["graph_index_range_inclusive"].rewrite_case_schema(case, schema)
    assert g.queries["graph"] == "SELECT id FROM n1 WHERE f1 >= :p"
    assert g.queries["graph:scan"] == case.queries["graph:scan"] and g.queries["graph:rev"] == case.queries["graph:rev"]
    r = MUTANTS_BY_NAME["rel_index_null_keys"].rewrite_case_schema(case, schema)
    assert r.queries["rel"] == "SELECT id FROM n1 WHERE (f1 = :p OR f1 IS NULL)"
    d = MUTANTS_BY_NAME["doc_index_desc_walk"].rewrite_case_schema(case, schema)
    assert d.queries["doc"].endswith("ORDER BY f1 ASC")
    # no index, no corruption
    schema0, _ = _fixture()
    assert MUTANTS_BY_NAME["graph_index_range_inclusive"].rewrite_case_schema(case, schema0) is case


def test_poly_join_over_a_sibling_list_field_is_o5():
    schema = Schema(
        node_types=(NodeTypeSpec("n1", (FieldSpec("f1", FieldType.INT),)), NodeTypeSpec("n2", (FieldSpec("f1", FieldType.LIST),))),
        edge_types=(),
        base=NodeTypeSpec("N0", ()),
    )
    inst = Instance(schema, [NodeRow("n1", 0, {"f1": 1}), NodeRow("n2", 0, {"f1": [1]})], [])
    for op in ("<=", ">", "=", "<>"):
        q = SelectQuery("n1", None, "join", ("f1",), fn=op, arg="count")
        # the list on the left (jrev) throws (the equalities unprobed: counted in); a
        # Long on the left answers false
        assert select_known(q, SelectSyntax(position="poly", form="jrev"), Rewrites(), "doc", schema, inst) == "O5", op
        assert select_known(q, SelectSyntax(position="poly"), Rewrites(), "doc", schema, inst) is None
        assert select_known(q, SelectSyntax(form="jrev"), Rewrites(), "doc", schema, inst) is None


def test_mixed_compare_matrix_and_the_decimal_sibling():
    from ganda.orientdb.known_bugs import mixed_compare_throws

    D, L, I, T, B = FieldType.DECIMAL, FieldType.INT, FieldType.INT32, FieldType.DATETIME, FieldType.BOOL
    assert mixed_compare_throws(D, L, "=") and not mixed_compare_throws(D, I, "=")
    assert mixed_compare_throws(D, T, "<") and not mixed_compare_throws(D, T, "=")
    assert mixed_compare_throws(T, B, "<") and mixed_compare_throws(B, T, ">=") and not mixed_compare_throws(L, T, "<")
    schema = Schema(
        node_types=(NodeTypeSpec("n1", (FieldSpec("f1", FieldType.DECIMAL),)), NodeTypeSpec("n2", (FieldSpec("f1", FieldType.INT),))),
        edge_types=(),
        base=NodeTypeSpec("N0", ()),
    )
    inst = Instance(schema, [NodeRow("n1", 0, {"f1": 1.5}), NodeRow("n2", 0, {"f1": 1})], [])
    q = SelectQuery("n2", None, "join", ("f1",), fn="=", arg="ids")
    # seed 3: under jrev the sibling's DECIMAL is on the left of this class's LONG
    assert select_known(q, SelectSyntax(position="poly", form="jrev"), Rewrites(), "rel", schema, inst) == "O5"
    assert select_known(q, SelectSyntax(position="poly"), Rewrites(), "rel", schema, inst) is None
    # and the DECIMAL class's own join has its DECIMAL on the left in the forward form
    q1 = SelectQuery("n1", None, "join", ("f1",), fn="<>", arg="count")
    assert select_known(q1, SelectSyntax(position="polyin"), Rewrites(), "rel", schema, inst) == "O5"


def test_o17_covers_a_map_entry_between():
    from ganda.orientdb.known_bugs import o17_predicate

    schema = Schema(node_types=(NodeTypeSpec("n1", (FieldSpec("m", FieldType.MAP),)),), edge_types=())
    lossy = Predicate("source", "m", "BETWEEN", "p", [-54, -49.5], ftype="int", key="k3")
    exact = Predicate("source", "m", "BETWEEN", "p", [-54, -49], ftype="int", key="k3")
    assert o17_predicate(schema, "n1", lossy) and not o17_predicate(schema, "n1", exact)


def test_scan_verdict_is_not_masked_by_an_index_finding():
    # a semi-join with a null answered value (O14 where served) next to a reversed
    # date leaf with a string pivot (O32 on every data model): the scan side is O32
    ix = IndexSpec("n1", "f2", data_model="graph")
    schema = Schema(
        node_types=(NodeTypeSpec("n1", (FieldSpec("f2", FieldType.STRING), FieldSpec("d", FieldType.DATETIME))), NodeTypeSpec("n2", (FieldSpec("g", FieldType.STRING),))),
        edge_types=(),
        indexes=(ix,),
    )
    inst = Instance(schema, [NodeRow("n1", 0, {"f2": "a", "d": "2020-01-01 00:00:00"}), NodeRow("n2", 0, {})], [])
    sub = Predicate("source", "f2", "INSUB", "", [None], True, "string", sub=("n2", "g", None, False))
    rev = Predicate("source", "d", "<", "p_1", "2021-01-01 00:00:00", ftype="datetime", rev=True)
    q = SelectQuery("n1", Compound("source", "AND", (sub, rev)), "ids")
    assert select_known(q, BASE, Rewrites(), "graph", schema, inst) == "O14"
    assert select_known(q, SelectSyntax(position="scan"), Rewrites(), "graph", schema, inst) == "O32"
