from ganda.orientdb.generate import GeneratorConfig, build
from ganda.orientdb.known_bugs import o37_write, write_known
from ganda.core.model import EdgeRow, EdgeTypeSpec, FieldSpec, FieldType, IndexSpec, Instance, NodeRow, NodeTypeSpec, Schema
from ganda.orientdb.queries.predicates import Predicate
from ganda.core.generators.write import sample_write, unique_safe
from ganda.core.ast.write import COVERAGE_DENOMINATOR, WriteQuery, all_coverage_signatures, answer, apply, coverage_signature, from_params
from ganda.orientdb.queries.write_syntax import WriteSyntax, WriteRewrites, applicable, render
from ganda.core.generators.write_phase import build_write_case

from ganda.orientdb import Schema  # noqa: E402,F811 -- bound to the OrientDB provider

CFG = GeneratorConfig(inheritance_probability=0.5, narrow_type_probability=0.3, date_map_probability=0.5, collate_probability=0.4, list_index_probability=0.5, custom_key_probability=0.3)


def _fixture(indexes=()):
    schema = Schema(
        node_types=(NodeTypeSpec("n1", (FieldSpec("f1", FieldType.INT), FieldSpec("f2", FieldType.STRING))),),
        edge_types=(EdgeTypeSpec("e1", "n1", "n1", (FieldSpec("w", FieldType.INT),)),),
        indexes=tuple(indexes),
    )
    nodes = [NodeRow("n1", 0, {"f1": 1, "f2": "a"}), NodeRow("n1", 1, {"f1": 5}), NodeRow("n1", 2, {"f1": 8, "f2": "b"}), NodeRow("n1", 3, {})]
    edges = [EdgeRow("e1", 0, "n1/r0", "n1/r1", {"w": 1}), EdgeRow("e1", 1, "n1/r1", "n1/r2", {"w": 7}), EdgeRow("e1", 2, "n1/r2", "n1/r0", {})]
    return schema, Instance(schema, nodes, edges)


def _gt(field, v, on="source"):
    return Predicate(on, field, ">", "p", v, ftype="int")


def test_apply_and_answer():
    schema, inst = _fixture()
    q = WriteQuery("arith", table="n1", where=_gt("f1", 4), field="f1", value=1, sym="+", returning="after")
    after = apply(q, inst)
    assert [n.values.get("f1") for n in after.nodes] == [1, 6, 9, None]
    assert answer(q, inst) == [{"id": "n1/r1", "v": 6}, {"id": "n1/r2", "v": 9}]
    assert answer(WriteQuery("arith", table="n1", where=None, field="f1", value=1), inst) == [{"count": 3}]  # the null row is not an operand
    rm = apply(WriteQuery("remove", table="n1", where=_gt("f1", 4), field="f1"), inst)
    assert "f1" not in rm.nodes[1].values and rm.nodes[0].values["f1"] == 1
    nu = apply(WriteQuery("null", table="n1", where=None, field="f2"), inst)
    assert all("f2" in n.values and n.values["f2"] is None for n in nu.nodes)
    # a delete takes every incident relationship with it
    de = apply(WriteQuery("delete", table="n1", where=_gt("f1", 6)), inst)
    assert [n.record_id for n in de.nodes] == ["n1/r0", "n1/r1", "n1/r3"] and [e.record_id for e in de.edges] == ["e1/e0"]
    # an upsert of a new id inserts the id and the field only
    up = WriteQuery("upsert", table="n1", field="f2", value="z", target_id="n1/w0", new_key="w0")
    assert apply(up, inst).nodes[-1].record_id == "n1/w0" and apply(up, inst).nodes[-1].values == {"f2": "z"}
    assert answer(up, inst) == [{"count": 1}]
    ed = WriteQuery("edge_delete", edge_type="e1", where=Predicate("edge", "w", ">", "p", 0, ftype="int"))
    assert [e.record_id for e in apply(ed, inst).edges] == ["e1/e2"] and answer(ed, inst) == [{"count": 2}]
    q2 = from_params(q.to_params())
    assert q2 == q


def test_spellings_per_model():
    schema, inst = _fixture()
    tw = WriteRewrites()
    q = WriteQuery("set", table="n1", where=_gt("f1", 4), field="f2", value="x", returning="before")
    after = apply(q, inst)
    st = render(q, WriteSyntax(target="nest"), tw, "doc", inst, after)
    assert st.main[0] == "UPDATE n1 SET f2 = :wv RETURN BEFORE $current.id AS id, $current.f2 AS v WHERE id IN (SELECT id FROM n1 WHERE f1 > :p) TIMEOUT 10000 EXCEPTION"
    assert st.main[1] == {"p": 4, "wv": "x"}  # the filter's bind travels with the statement
    st = render(q, WriteSyntax(target="sub", door="tx"), tw, "graph", inst, after)
    assert st.script().startswith("BEGIN; LET $r = UPDATE (SELECT FROM n1 WHERE f1 > :p) SET f2 = :wv") and st.script().endswith("COMMIT; RETURN $r")
    d = WriteQuery("delete", table="n1", where=_gt("f1", 6))
    da = apply(d, inst)
    g = render(d, WriteSyntax(), tw, "graph", inst, da)
    assert g.main[0] == "DELETE VERTEX n1 WHERE f1 > :p" and not g.prelude
    r = render(d, WriteSyntax(), tw, "rel", inst, da)
    assert r.prelude[0][0].startswith("DELETE FROM e1 WHERE src_id IN (SELECT id FROM n1 WHERE f1 > :p) OR dst_id IN") and r.main[0] == "DELETE FROM n1 WHERE f1 > :p"
    doc = render(d, WriteSyntax(form="eids"), tw, "doc", inst, da)
    assert doc.prelude[0][1]["wg_e1"] == ["e1/e1"] and "eid NOT IN :wg_e1" in doc.prelude[0][0]
    # the doc data model's edge-attribute write rewrites each source's list by id; it answers nothing compared
    ew = WriteQuery("edge_set", edge_type="e1", where=Predicate("edge", "w", ">", "p", 0, ftype="int"), field="w", value=9)
    ea = apply(ew, inst)
    dd = render(ew, WriteSyntax(form="rewrite"), tw, "doc", inst, ea)
    assert dd.main is None and [p["wi0"] if "wi0" in p else p["wi1"] for _t, p in dd.prelude] == ["n1/r0", "n1/r1"]
    assert render(ew, WriteSyntax(form="update_edge"), tw, "graph", inst, ea).main[0].startswith("UPDATE EDGE e1 SET w = :wv WHERE w > :p")
    assert not applicable(WriteQuery("set", table="n1", where=None, field="f1", value=1), WriteSyntax(target="nest"), tw, "doc")
    assert not applicable(ew, WriteSyntax(form="rewrite", target="nest"), tw, "doc")


def test_o37_guard_names_the_moving_write_through_a_served_range():
    ix = IndexSpec("n1", "f1", data_model="graph", kind="NOTUNIQUE")
    schema, inst = _fixture([ix])
    q = WriteQuery("arith", table="n1", where=_gt("f1", 4), field="f1", value=1)
    assert o37_write(q, WriteSyntax(), "graph", q.where, schema)
    assert not o37_write(q, WriteSyntax(), "doc", q.where, schema)  # no index there
    assert not o37_write(q, WriteSyntax(target="nest"), "graph", q.where, schema)
    assert not o37_write(q, WriteSyntax(door="tx"), "graph", q.where, schema)
    assert o37_write(q, WriteSyntax(target="sub"), "graph", q.where, schema)
    assert not o37_write(WriteQuery("remove", table="n1", where=_gt("f1", 4), field="f1"), WriteSyntax(), "graph", q.where, schema)
    eq = Predicate("source", "f1", "=", "p", 5, ftype="int")
    assert not o37_write(WriteQuery("arith", table="n1", where=eq, field="f1", value=1), WriteSyntax(), "graph", eq, schema)
    # the guard redraws: some concrete syntax of the graph data model stays safe
    assert write_known(q, WriteSyntax(), WriteRewrites(), "graph", schema, inst) == "O37"
    assert write_known(q, WriteSyntax(target="nest"), WriteRewrites(), "graph", schema, inst) is None


def test_unique_columns_are_written_through_one_record_only():
    ix = IndexSpec("n1", "f1", data_model="rel", kind="UNIQUE", ignore_nulls=True)
    schema, inst = _fixture([ix])
    many = WriteQuery("arith", table="n1", where=None, field="f1", value=1)
    assert not unique_safe(many, inst, apply(many, inst))
    one = WriteQuery("set", table="n1", where=Predicate("source", "f1", "=", "p", 5, ftype="int"), field="f1", value=8)
    assert not unique_safe(one, inst, apply(one, inst))  # 8 is taken
    fresh = WriteQuery("set", table="n1", where=Predicate("source", "f1", "=", "p", 5, ftype="int"), field="f1", value=9)
    assert unique_safe(fresh, inst, apply(fresh, inst))


def test_sampled_writes_are_well_formed_and_deterministic():
    seen = set()
    for seed in range(1000, 1012):
        schema, inst, rng = build(seed, CFG)
        r = rng.for_generator("ir_write")
        for step in range(8):
            drawn = sample_write(r, schema, inst, step)
            if drawn is None:
                continue
            q, rewrites, syntaxes = drawn
            after = apply(q, inst)
            case, statements = build_write_case(q, rewrites, syntaxes, step, inst, after, False)
            assert not case.known_bugs  # every data model drew a concrete syntax no guard names
            assert unique_safe(q, inst, after)
            for data_model, st in statements.items():
                for text, params in st.prelude + ([st.main] if st.main else []):
                    for name in __import__("re").findall(r"(?<![\w:]):([A-Za-z_]\w*)", text):
                        assert name in params, (data_model, text, name)
            seen.add(coverage_signature(q))
            inst = after
    assert len(seen) > 20 and seen <= set(all_coverage_signatures()) and COVERAGE_DENOMINATOR == len(all_coverage_signatures())
    a = sample_write(build(1003, CFG)[2].for_generator("ir_write"), *build(1003, CFG)[:2], 0)
    b = sample_write(build(1003, CFG)[2].for_generator("ir_write"), *build(1003, CFG)[:2], 0)
    assert a[0] == b[0] and {s: sp for s, (sp, _k) in a[2].items()} == {s: sp for s, (sp, _k) in b[2].items()}


def test_write_phase_draws_nothing_from_the_read_streams():
    from ganda.core.generators.registry import all_cases

    schema, inst, rng = build(1003, CFG)
    before = [c.queries for c in all_cases(rng, schema, inst, traversal_cases=5, select_cases=5)]
    schema, inst, rng = build(1003, CFG)
    sample_write(rng.for_generator("ir_write"), schema, inst, 0)
    assert [c.queries for c in all_cases(rng, schema, inst, traversal_cases=5, select_cases=5)] == before
