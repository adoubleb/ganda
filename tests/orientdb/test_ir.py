import itertools

from ganda.orientdb.facade.generate import GeneratorConfig, build
from ganda.core.data.model import EdgeRow, EdgeTypeSpec, FieldSpec, FieldType, Instance, NodeRow, NodeTypeSpec, Schema
from ganda.core.query.ir.traversal import PROJECTIONS, SHAPE_DENOMINATOR, Hop, TraversalQuery, all_signatures, evaluate, from_params, shape_signature
from ganda.core.query.generators.traversal import generate_traversal_cases, sample_query
from ganda.orientdb.facade.predicates import Compound, Predicate
from ganda.orientdb.concrete_syntax.traversal import SYNTAXES, applicable, can_pull_up, render, syntaxes_for


from ganda.orientdb import Schema  # noqa: E402,F811 -- bound to the OrientDB provider

def _fixture():
    schema = Schema(
        node_types=(NodeTypeSpec("n1", (FieldSpec("f1", FieldType.INT), FieldSpec("f2", FieldType.STRING))), NodeTypeSpec("n2", (FieldSpec("f1", FieldType.INT),))),
        edge_types=(EdgeTypeSpec("e1", "n1", "n1", (FieldSpec("w", FieldType.INT),)), EdgeTypeSpec("e2", "n1", "n2", ())),
    )
    nodes = [NodeRow("n1", 0, {"f1": 0, "f2": "a"}), NodeRow("n1", 1, {"f1": 1}), NodeRow("n1", 2, {"f1": 2, "f2": "c"}), NodeRow("n2", 0, {"f1": 5})]
    edges = [
        EdgeRow("e1", 0, "n1/r0", "n1/r1", {"w": 1}),
        EdgeRow("e1", 1, "n1/r0", "n1/r1", {"w": 2}),  # parallel
        EdgeRow("e1", 2, "n1/r0", "n1/r0", {"w": 3}),  # self-loop
        EdgeRow("e1", 3, "n1/r1", "n1/r2", {}),
        EdgeRow("e2", 0, "n1/r2", "n2/r0", {}),
    ]
    return schema, Instance(schema, nodes, edges)


def test_evaluate_multiplicity_and_null_predicate():
    schema, inst = _fixture()
    q = TraversalQuery("n1", (Hop("e1", "out"),), "ids")
    assert evaluate(q, inst) == {"n1/r0": ["n1/r1", "n1/r1", "n1/r0"], "n1/r1": ["n1/r2"], "n1/r2": []}
    q = TraversalQuery("n1", (Hop("e1", "out", Predicate("edge", "w", ">", "p1", 1)),), "count")
    assert evaluate(q, inst) == {"n1/r0": 2, "n1/r1": 0, "n1/r2": 0}  # the null w edge fails `> 1`
    q = TraversalQuery("n1", (Hop("e1", "out", Predicate("edge", "w", "<>", "p1", 1)),), "count")
    assert evaluate(q, inst)["n1/r1"] == 1  # null <> 1 holds
    q = TraversalQuery("n1", (Hop("e1", "in"),), "ids")
    assert evaluate(q, inst)["n1/r1"] == ["n1/r0", "n1/r0"]
    q = TraversalQuery("n1", (Hop("e1", "out"), Hop("e2", "out")), "ids")
    assert evaluate(q, inst) == {"n1/r0": [], "n1/r1": ["n2/r0"], "n1/r2": []}
    q = TraversalQuery("n1", (Hop("e1", "out"),), "exists")
    assert evaluate(q, inst) == ["n1/r0", "n1/r1"]
    q = TraversalQuery("n1", (Hop("e1", "out"),), "max", "f1")
    assert evaluate(q, inst) == {"n1/r0": 1, "n1/r1": 2, "n1/r2": None}


def test_params_round_trip():
    q = TraversalQuery("n1", (Hop("e1", "out", Predicate("edge", "w", "IN", "p1", [1, 2])), Hop("e2", "out")), "field", "f1", Predicate("source", "f2", "LIKE", "p0", "a%"))
    assert from_params(q.to_params()) == q
    assert shape_signature(q) == "src=f hops=2[out:e,out:-] proj=field"


def test_shape_denominator():
    assert SHAPE_DENOMINATOR == len(set(all_signatures()))
    # `both` is a one-hop direction: 3 directions x 3 placements at one hop, 2 x 3 after;
    # the four reach projections count for the one unpredicated `out` hop only
    assert SHAPE_DENOMINATOR == (9 + 36 + 216) * 2 * (len(PROJECTIONS) - 4) + 2 * 4


def test_every_admitted_syntax_renders_every_data_model():
    schema, inst = _fixture()
    preds = [None, Predicate("edge", "w", ">", "p1", 1), Predicate("target", "f1", "<=", "p1", 1)]
    for n in (1, 2):
        for dirs in itertools.product(("out", "in", "both") if n == 1 else ("out", "in"), repeat=n):
            for pred in preds:
                hops = tuple(Hop("e1", d, pred if k == 0 else None) for k, d in enumerate(dirs))
                for proj in PROJECTIONS:
                    for sp in (None, Predicate("source", "f1", ">=", "p0", 0), Compound("source", "OR", (Predicate("source", "f1", ">=", "p0_0", 0), Predicate("source", "f2", "IS NULL", "", None)))):
                        field = "f1" if proj in ("field", "max", "min", "sum") else ("w" if proj.startswith("e") and proj != "exists" else None)
                        q = TraversalQuery("n1", hops, proj, field, sp)
                        for data_model, names in SYNTAXES.items():
                            for name in names:
                                if applicable(q, schema, data_model, name):
                                    for pull_up in ((False, True) if can_pull_up(q) else (False,)):
                                        text = render(q, schema, data_model, name, pull_up=pull_up)
                                        assert text.upper().startswith(("SELECT", "MATCH")), (data_model, name, text)
                                        for b in q.bind_vars:
                                            assert f":{b}" in text, (data_model, name, b, text)
                                        if pull_up:
                                            assert text.startswith("SELECT id") and " FROM (" in text


def test_rendered_texts_for_one_question():
    schema, inst = _fixture()
    q = TraversalQuery("n1", (Hop("e1", "out", Predicate("edge", "w", ">", "p1", 1)),), "ids")
    assert render(q, schema, "graph", "chain") == "SELECT id, outE('e1')[w > :p1].inV().id AS v FROM n1"
    assert render(q, schema, "doc", "path") == "SELECT id, e1[w > :p1].target.id AS v FROM n1"
    assert render(q, schema, "rel", "let") == "SELECT id, $h0.v AS v FROM n1 LET $h0 = (SELECT dst_id AS v FROM e1 WHERE src_id = $parent.$current.id AND w > :p1)"
    assert render(q, schema, "rel", "flat") == "SELECT src_id AS id, dst_id AS v FROM e1 WHERE w > :p1"
    assert render(q, schema, "graph", "match") == "MATCH {class: n1, as: a} .outE('e1') {as: ae1, where: (w > :p1)} .inV() {as: av1} RETURN a.id AS id, av1.id AS v"


def test_sampler_only_keeps_questions_every_data_model_can_render():
    for seed in range(1, 30):
        schema, inst, rng = build(seed, GeneratorConfig())
        for case in generate_traversal_cases(rng.for_generator("ir_traversal"), schema, inst):
            q = from_params(case.params["traversal"])
            for data_model in ("doc", "graph", "rel"):
                assert syntaxes_for(q, schema, data_model)
                assert data_model in case.queries
            assert case.shape in all_signatures()


def test_sampler_is_deterministic():
    schema, inst, rng = build(9, GeneratorConfig())
    a = [c.queries for c in generate_traversal_cases(rng.for_generator("ir_traversal"), schema, inst)]
    schema, inst, rng = build(9, GeneratorConfig())
    b = [c.queries for c in generate_traversal_cases(rng.for_generator("ir_traversal"), schema, inst)]
    assert a == b


def test_edge_projections_and_paths():
    schema, inst = _fixture()
    q = TraversalQuery("n1", (Hop("e1", "out"),), "efield", "w")
    assert evaluate(q, inst) == {"n1/r0": [1, 2, 3], "n1/r1": [None], "n1/r2": []}
    q = TraversalQuery("n1", (Hop("e1", "in"),), "emax", "w")
    assert evaluate(q, inst) == {"n1/r0": 3, "n1/r1": 2, "n1/r2": None}
    q = TraversalQuery("n1", (Hop("e1", "out"), Hop("e1", "out")), "esum", "w")
    assert evaluate(q, inst) == {"n1/r0": 6, "n1/r1": 0, "n1/r2": 0}
    assert render(q, schema, "graph", "chain") == "SELECT id, sum(out('e1').outE('e1').w) AS v FROM n1 GROUP BY id"
    assert render(TraversalQuery("n1", (Hop("e1", "out"),), "efield", "w"), schema, "rel", "flat") == "SELECT src_id AS id, w AS v FROM e1"
    assert not applicable(TraversalQuery("n1", (Hop("e1", "out", Predicate("target", "f1", ">", "p1", 0)),), "efield", "w"), schema, "graph", "chain")


def test_compound_predicates_render_and_evaluate():
    schema, inst = _fixture()
    p = Compound("edge", "NOT", (Compound("edge", "OR", (Predicate("edge", "w", "<", "p1_0_0", 2), Predicate("edge", "w", "IS NULL", "", None))),))
    q = TraversalQuery("n1", (Hop("e1", "out", p),), "count")
    assert evaluate(q, inst) == {"n1/r0": 2, "n1/r1": 0, "n1/r2": 0}
    assert render(q, schema, "doc", "path") == "SELECT id, e1[NOT ((w < :p1_0_0 OR w IS NULL))].target.size() AS v FROM n1"
    assert from_params(q.to_params()) == q
    assert q.bind_vars == {"p1_0_0": 2}


def test_pulled_up_form_and_traverse():
    schema, inst = _fixture()
    sp = Predicate("source", "f1", ">", "p0", 0)
    q = TraversalQuery("n1", (Hop("e2", "out"),), "distinct_ids", None, sp)
    assert render(q, schema, "graph", "chain", pull_up=True) == "SELECT id, v FROM (SELECT id, f1, out('e2').id.asSet() AS v FROM n1) WHERE f1 > :p0"
    assert render(q, schema, "rel", "flat", pull_up=True) == "SELECT id, v FROM (SELECT DISTINCT src_id AS id, dst_id AS v FROM e2) WHERE id IN (SELECT id FROM n1 WHERE f1 > :p0)"
    assert applicable(q, schema, "graph", "traverse")
    assert not applicable(TraversalQuery("n1", (Hop("e1", "out"),), "distinct_ids"), schema, "graph", "traverse")  # e1 is self-typed
    assert not can_pull_up(TraversalQuery("n1", (Hop("e2", "out"),), "ids", None, Predicate("source", "f1", "IS DEFINED", "", None)))


def test_both_direction_nexists_and_optional_match():
    schema, inst = _fixture()
    q = TraversalQuery("n1", (Hop("e1", "both"),), "ids")
    # a self-loop is visited twice, parallel edges twice, the incoming data model once more
    assert evaluate(q, inst) == {"n1/r0": ["n1/r1", "n1/r1", "n1/r0", "n1/r0"], "n1/r1": ["n1/r2", "n1/r0", "n1/r0"], "n1/r2": ["n1/r1"]}
    assert render(q, schema, "graph", "chain") == "SELECT id, both('e1').id AS v FROM n1"
    assert render(q, schema, "graph", "bag") == "SELECT id, unionall(out_e1.in, in_e1.out).id AS v FROM n1"
    assert render(q, schema, "rel", "let") == "SELECT id, unionall($h0.v, $g0.v) AS v FROM n1 LET $h0 = (SELECT dst_id AS v FROM e1 WHERE src_id = $parent.$current.id), $g0 = (SELECT src_id AS v FROM e1 WHERE dst_id = $parent.$current.id)"
    assert render(q, schema, "doc", "union").startswith("SELECT id, unionall(e1.target.id, $s.v) AS v FROM n1 LET $s = (")
    assert render(q, schema, "graph", "edgescan") == "SELECT expand(unionall((SELECT out.id AS id, in.id AS v FROM e1), (SELECT in.id AS id, out.id AS v FROM e1)))"
    ep = Predicate("edge", "w", ">", "p1", 1)
    assert not applicable(TraversalQuery("n1", (Hop("e1", "both", ep),), "ids"), schema, "graph", "match")
    assert applicable(TraversalQuery("n1", (Hop("e1", "both", ep),), "efield", "w"), schema, "graph", "match")
    assert render(TraversalQuery("n1", (Hop("e1", "both", ep),), "efield", "w"), schema, "graph", "match") == "MATCH {class: n1, as: a} .bothE('e1') {as: ae1, where: (w > :p1)} RETURN a.id AS id, ae1.w AS v"
    assert not applicable(TraversalQuery("n1", (Hop("e1", "both"), Hop("e1", "out")), "ids"), schema, "graph", "chain")
    # nexists: the complement of exists; the anti-join on the relational data model
    nq = TraversalQuery("n1", (Hop("e1", "out"),), "nexists")
    assert evaluate(nq, inst) == ["n1/r2"]
    assert render(nq, schema, "graph", "chain") == "SELECT id FROM n1 WHERE out('e1').size() = 0"
    assert render(nq, schema, "rel", "flat") == "SELECT id FROM n1 WHERE id NOT IN (SELECT src_id FROM e1)"
    assert not applicable(nq, schema, "graph", "match")
    # optional MATCH: every alias optional
    oq = TraversalQuery("n1", (Hop("e1", "out", ep),), "ids")
    assert render(oq, schema, "graph", "match_opt") == "MATCH {class: n1, as: a} .outE('e1') {as: ae1, where: (w > :p1), optional: true} .inV() {as: av1, optional: true} RETURN a.id AS id, av1.id AS v"


def test_narrow_types_in_the_reference():
    from ganda.orientdb.oracle.semantics import agg, predicate_hit, sum_is_exact

    # an INTEGER column: a Long pivot compares exactly on a scan; the wrap is the index's (O16)
    assert not predicate_hit(True, 5, "=", 2**32 + 5, "int32")
    assert predicate_hit(True, 5, "<", 2**31, "int32")
    # a FLOAT column: an integral pivot compares through float, a double through double
    assert predicate_hit(True, 16777216.0, "=", 16777217, "float32")
    assert not predicate_hit(True, 16777216.0, "=", 16777217.0, "float32")
    assert not predicate_hit(True, 0.125, "=", 0.1, "float32")
    # a DECIMAL column: a double pivot through its binary expansion, BETWEEN through its string
    assert not predicate_hit(True, 0.1, "=", 0.1, "decimal")
    assert predicate_hit(True, 0.5, "=", 0.5, "decimal")
    assert predicate_hit(True, 0.1, "BETWEEN", [0.1, 0.5], "decimal")
    assert not predicate_hit(True, 0.1, ">=", 0.1, "decimal")
    assert predicate_hit(True, 0.1, "<>", 0.1, "decimal")
    assert predicate_hit(True, 5.0, "=", 5, "decimal")
    # ci: `=` and `<` lower-case, IN and BETWEEN do not, LIKE always does
    assert predicate_hit(True, "Ab", "=", "aB", "string", True)
    assert not predicate_hit(True, "Ab", "=", "aB", "string", False)
    assert not predicate_hit(True, "Ab", "IN", ["aB"], "string", True)
    assert predicate_hit(True, "Ab", "LIKE", "a%", "string", False)
    assert predicate_hit(True, "Ab", "LOWER=", "ab", "string", True)
    # sums: 32-bit for an INTEGER column, exact decimal arithmetic for a DECIMAL one
    assert sum_is_exact([2**31 - 1, 5], "int") and not sum_is_exact([2**31 - 1, 5], "int32")
    assert agg("sum", [0.1, 0.2], "decimal") == 0.3


def test_reach_projections():
    from ganda.core.query.ir.traversal import REACH_PROJECTIONS

    schema, inst = _fixture()
    q = TraversalQuery("n1", (Hop("e1", "out"),), "reach2")
    # r0 -> r1 -> r2 (two hops), the self-loop keeps r0; r2 has no e1 out-edge
    assert evaluate(q, inst) == {"n1/r0": ["n1/r0", "n1/r1", "n1/r2"], "n1/r1": ["n1/r1", "n1/r2"], "n1/r2": ["n1/r2"]}
    assert evaluate(TraversalQuery("n1", (Hop("e1", "out"),), "reach3_count"), inst) == {"n1/r0": 3, "n1/r1": 2, "n1/r2": 1}
    for proj in REACH_PROJECTIONS:
        q = TraversalQuery("n1", (Hop("e1", "out"),), proj)
        for data_model in ("doc", "graph", "rel"):
            assert syntaxes_for(q, schema, data_model), (proj, data_model)
        assert not applicable(q, schema, "graph", "edgescan") and not applicable(q, schema, "rel", "flat")
    assert render(TraversalQuery("n1", (Hop("e1", "out"),), "reach2"), schema, "graph", "chain") == "SELECT id, unionall([@this], out('e1'), out('e1').out('e1')).id.asSet() AS v FROM n1"
    assert render(TraversalQuery("n1", (Hop("e1", "out"),), "reach2_count"), schema, "graph", "traverse") == "SELECT id, $t.id.asSet().size() AS v FROM n1 LET $t = (SELECT FROM (TRAVERSE out('e1') FROM $parent.$current MAXDEPTH 2 STRATEGY BREADTH_FIRST))"
    assert "$h2" in render(TraversalQuery("n1", (Hop("e1", "out"),), "reach2"), schema, "rel", "let")
    assert render(TraversalQuery("n1", (Hop("e1", "out"),), "reach2_count"), schema, "doc", "path").count("UNWIND v") == 2


def test_dates_and_maps_in_the_reference():
    from ganda.orientdb.oracle.semantics import canonical_date, predicate_hit

    assert canonical_date("2024-01-01") == "2024-01-01 00:00:00"
    assert canonical_date(1704112496000) == "2024-01-01 12:34:56"
    assert canonical_date(1704112496500) == "2024-01-01 12:34:56"
    assert canonical_date("2024-01-01T12:34:56") == "2024-01-01 12:34:56"
    assert canonical_date("2024-01-01 12:34") == "2024-01-01 12:34:00"
    assert predicate_hit(True, "2024-01-01 00:00:00", "=", "2024-01-01", "datetime")
    assert predicate_hit(True, "2024-01-01 12:34:56", ">", "2024-01-01", "datetime")
    assert predicate_hit(True, "2024-01-01 12:34:56", "=", 1704112496000, "datetime")
    assert predicate_hit(True, "2024-01-01 00:00:00", "BETWEEN", ["2024-01-01", "2024-01-01 12:00:00"], "date")
    assert not predicate_hit(True, "2024-01-01 00:00:00", "=", "2024-01-01 12:00:00", "date")
    m = {"k1": 1, "k2": 2}
    assert predicate_hit(True, m, "CONTAINSKEY", "k2") and not predicate_hit(True, m, "CONTAINSKEY", "k3")
    assert predicate_hit(True, m, "VALUES CONTAINS", 2) and not predicate_hit(True, m, "CONTAINSVALUE", 2)  # O7's map face
    assert predicate_hit(True, m, "=", 2, key="k2") and predicate_hit(True, m, "IS NULL", None, key="k3")
    assert predicate_hit(False, None, "IS NULL", None, key="k3") and predicate_hit(True, m, "<>", 5, key="k3")
    assert not predicate_hit(True, m, ">", 1, key="k3")
    p = Predicate("source", "m", "=", "p", 2, True, "int", False, "k2")
    from ganda.orientdb.concrete_syntax.predicate import render_predicate as render_pred

    assert render_pred(p) == "m.k2 = :p"
    assert render_pred(Predicate("source", "m", "=", "p", 2, True, "int", False, "k2", "pk")) == "m[:pk] = :p" and Predicate("source", "m", "=", "p", 2, True, "int", False, "k2", "pk").bind_vars == {"pk": "k2", "p": 2}
    assert render_pred(Predicate("source", "m", "VALUES CONTAINS", "p", 2)) == "m.values() CONTAINS :p"
