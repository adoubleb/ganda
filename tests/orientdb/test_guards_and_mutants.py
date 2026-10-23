from ganda.orientdb.facade.generate import GeneratorConfig, build
from ganda.orientdb.known_bugs.guards import o1_shape, o2_shape
from ganda.core.data.model import EdgeTypeSpec, FieldSpec, FieldType, IndexSpec, Instance, NodeTypeSpec, Schema
from ganda.orientdb.mutation.mutants import MUTANTS
from ganda.core.query.ir.traversal import Hop, TraversalQuery
from ganda.orientdb.facade.predicates import Predicate
from ganda.core.query.generators.registry import all_cases


from ganda.orientdb import Schema  # noqa: E402,F811 -- bound to the OrientDB provider

def _schema(indexes=()):
    return Schema(
        node_types=(NodeTypeSpec("n1", (FieldSpec("f1", FieldType.INT),)),),
        edge_types=(EdgeTypeSpec("e1", "n1", "n1", (FieldSpec("w", FieldType.INT),)),),
        indexes=tuple(indexes),
    )


def test_o1_shape_is_correlated_plus_indexed_source_predicate():
    sp = Predicate("source", "f1", ">", "p0", 0)
    q = TraversalQuery("n1", (Hop("e1", "out"),), "ids", None, sp)
    assert o1_shape(q, _schema([IndexSpec("n1", "f1", data_model="rel", kind="NOTUNIQUE")]), "rel", "let")
    assert not o1_shape(q, _schema([IndexSpec("n1", "f1", data_model="doc", kind="NOTUNIQUE")]), "rel", "let")
    assert not o1_shape(q, _schema([IndexSpec("n1", "f1", data_model="rel", kind="NOTUNIQUE")]), "graph", "chain")
    assert not o1_shape(TraversalQuery("n1", (Hop("e1", "out"),), "ids"), _schema([IndexSpec("n1", "f1", data_model="rel", kind="NOTUNIQUE")]), "rel", "let")
    # flat: an IN-subquery predicate on the FK next to a correlated LET
    assert o1_shape(TraversalQuery("n1", (Hop("e1", "out"),), "field", "f1", sp), _schema(), "rel", "flat")
    assert not o1_shape(TraversalQuery("n1", (Hop("e1", "out"),), "ids", None, sp), _schema(), "rel", "flat")


def test_o2_shape_is_correlated_plus_indexed_second_predicate():
    tp = Predicate("target", "f1", ">", "p1", 0)
    q = TraversalQuery("n1", (Hop("e1", "out", tp),), "ids")
    assert o2_shape(q, _schema([IndexSpec("n1", "f1", data_model="both", kind="NOTUNIQUE")]), "rel", "let")
    assert not o2_shape(q, _schema(), "rel", "let")
    ep = Predicate("edge", "w", "=", "p1", 0)
    q = TraversalQuery("n1", (Hop("e1", "out", ep),), "ids")
    assert o2_shape(q, _schema([IndexSpec("e1", "w", data_model="rel", target="edge", kind="NOTUNIQUE")]), "rel", "let")
    assert not o2_shape(q, _schema([IndexSpec("e1", "w", data_model="graph", target="edge", kind="NOTUNIQUE")]), "rel", "let")
    null = Predicate("target", "f1", "IS NULL", "", None)
    assert o2_shape(TraversalQuery("n1", (Hop("e1", "out", null),), "ids"), _schema([IndexSpec("n1", "f1", data_model="both", kind="NOTUNIQUE")]), "rel", "let")


def test_known_variants_are_marked_on_generated_cases():
    marked = 0
    for seed in range(1, 40):
        schema, inst, rng = build(seed, GeneratorConfig(index_probability=0.9))
        for c in all_cases(rng, schema, inst):
            marked += len(c.known_bugs)
            for data_model in c.known_bugs:
                assert data_model in c.queries
    assert marked > 0


def test_mutants_change_something():
    schema, inst, _ = build(2, GeneratorConfig(churn_update_probability=0.5, churn_delete_probability=0.4, churn_node_delete_probability=0.4, churn_edge_update_probability=0.5))
    for m in MUTANTS:
        for data_model, fn in m.instance_for_data_model.items():
            out = fn(inst)
            changed = out.edges != inst.edges or out.nodes != inst.nodes or out.churn != inst.churn
            assert changed, m.name
    names = [m.name for m in MUTANTS]
    assert len(names) == len(set(names))


def test_every_read_generator_emits():
    schema, inst, rng = build(4, GeneratorConfig())
    cases = all_cases(rng, schema, inst, select_cases=30, path_cases=3)
    assert {c.generator_name for c in cases} == {"ir_traversal", "ir_select", "ir_path"}


def test_guards_see_leaves_inside_compounds():
    from ganda.orientdb.known_bugs.guards import node_case_known, o10_predicate, o12_literal
    from ganda.orientdb.facade.predicates import Compound

    schema = _schema([IndexSpec("n1", "f1", data_model="rel", kind="NOTUNIQUE")])
    inner = Predicate("target", "f1", ">", "p1_0", 0)
    tp = Compound("target", "OR", (Predicate("target", "f1", "IS NULL", "", None), inner))
    q = TraversalQuery("n1", (Hop("e1", "out", tp),), "ids")
    assert o2_shape(q, schema, "rel", "let")
    mixed = Compound("source", "AND", (Predicate("source", "f1", "IN", "p_0", [1, 2**40]), Predicate("source", "f1", "<>", "p_1", 5)))
    assert o10_predicate(schema, "n1", mixed, "rel") and not o10_predicate(schema, "n1", mixed, "doc")
    assert o12_literal(Compound("source", "NOT", (Predicate("source", "f2", "=", "p_0", 776.417),)))
    assert not o12_literal(Predicate("source", "f2", "=", "p", 1.5))
    schema2, inst, _ = build(3, GeneratorConfig())
    assert node_case_known("n1", None, schema2, inst) == {}


def test_pulled_up_variants_are_judged_without_the_source_predicate():
    from ganda.orientdb.known_bugs.guards import mark_known_bugs
    from ganda.core.oracle.case import Comparison, QueryCase

    schema = _schema([IndexSpec("n1", "f1", data_model="rel", kind="NOTUNIQUE")])
    sp = Predicate("source", "f1", ">", "p0", 0)
    q = TraversalQuery("n1", (Hop("e1", "out"),), "ids", None, sp)
    case = QueryCase("ir_traversal", "e1", Comparison.MULTISET_BY_ID, {"doc": "d", "graph": "g", "rel": "r", "rel:pull_up": "h"}, params={"concrete_syntax": {"doc": "path", "graph": "chain", "rel": "let", "rel:pull_up": "let"}, "pulled_up": ["rel:pull_up"]})
    inst = build(3, GeneratorConfig())[1]
    out = mark_known_bugs(case, q, schema, inst)
    assert out.known_bugs == {"rel": "O1"}


def test_cross_type_guards_o16_o17():
    from ganda.orientdb.known_bugs.guards import edge_case_known, node_case_known, o16_predicate, o17_predicate

    inst = build(3, GeneratorConfig())[1]
    schema = _schema([IndexSpec("n1", "f1", data_model="rel", kind="NOTUNIQUE_HASH_INDEX"), IndexSpec("n1", "f1", data_model="doc", kind="NOTUNIQUE")])
    eq = Predicate("source", "f1", "=", "p", 5.5)
    lt = Predicate("source", "f1", "<", "p", 5.5)
    assert o16_predicate(schema, "n1", eq, "rel") and o16_predicate(schema, "n1", eq, "doc") and not o16_predicate(schema, "n1", eq, "graph")
    assert o16_predicate(schema, "n1", lt, "doc") and not o16_predicate(schema, "n1", lt, "rel")  # a hash index scans a range
    assert not o16_predicate(schema, "n1", Predicate("source", "f1", "=", "p", 5.0), "doc")
    assert o16_predicate(schema, "n1", Predicate("source", "f1", "IN", "p", [1.0, 9007199254740992.0]), "doc")
    bt = Predicate("source", "f1", "BETWEEN", "p", [0.5, 5])
    assert o17_predicate(schema, "n1", bt) and not o17_predicate(schema, "n1", Predicate("source", "f1", "BETWEEN", "p", [1.0, 5.0]))
    assert node_case_known("n1", bt, schema, inst) == {"doc": "O17", "graph": "O17", "rel": "O17"}
    ebt = Predicate("edge", "w", "BETWEEN", "p", [0.5, 5])
    assert edge_case_known("e1", ebt, schema, inst) == {"doc": "O17", "graph": "O17", "rel": "O17"}
    assert edge_case_known("e1", Predicate("edge", "w", "=", "p", 5.5), _schema([IndexSpec("e1", "w", data_model="graph", target="edge", kind="NOTUNIQUE")]), inst) == {"graph": "O16"}


def test_o18_withholds_the_desc_order_variant_under_an_index_that_ignores_nulls():
    from ganda.orientdb.known_bugs.guards import select_shape_known
    from ganda.core.query.ir.select import SelectQuery
    from ganda.orientdb.concrete_syntax.select import SelectSyntax

    order = SelectSyntax(form="order")
    found = {"O18": 0, "plain": 0}
    for seed in range(1, 60):
        schema, inst, _ = build(seed, GeneratorConfig(index_probability=0.9))
        for nt in schema.node_types:
            for f in nt.fields:
                if f.type not in (FieldType.INT, FieldType.FLOAT) or not any(n.values.get(f.name) is not None for n in inst.nodes_of(nt.name)):
                    continue
                q = SelectQuery(nt.name, None, "agg", (f.name,), fn="max")
                present = [n.values[f.name] for n in inst.nodes_of(nt.name) if n.values.get(f.name) is not None]
                for data_model in ("doc", "graph", "rel"):
                    ignoring = any(ix.kind in ("NOTUNIQUE", "UNIQUE", "DICTIONARY") and ix.ignore_nulls for ix in schema.node_indexes(nt.name, f.name, data_model))
                    fid = select_shape_known(q, order, data_model, schema, inst, present)
                    if ignoring:
                        assert fid == "O18"
                        found["O18"] += 1
                    else:
                        assert fid is None
                        found["plain"] += 1
    assert found["O18"] > 0 and found["plain"] > 0


def test_new_type_and_collation_guards():
    from ganda.orientdb.known_bugs.guards import node_case_known, o11_predicate, o16_predicate, o17_predicate, o20_predicate, o21_predicate, ordered_keys_known
    from ganda.orientdb.facade.predicates import Compound

    inst = build(3, GeneratorConfig())[1]
    schema = Schema(
        node_types=(NodeTypeSpec("n1", (FieldSpec("i", FieldType.INT32), FieldSpec("f", FieldType.FLOAT32), FieldSpec("d", FieldType.DECIMAL), FieldSpec("s", FieldType.STRING, case_insensitive=True), FieldSpec("g", FieldType.INT))),),
        edge_types=(EdgeTypeSpec("e1", "n1", "n1", ()),),
        indexes=(IndexSpec("n1", "i", data_model="rel", kind="NOTUNIQUE"), IndexSpec("n1", "f", data_model="doc", kind="NOTUNIQUE_HASH_INDEX"), IndexSpec("n1", "d", data_model="graph", kind="NOTUNIQUE"), IndexSpec("n1", "s", data_model="rel", kind="NOTUNIQUE"), IndexSpec("n1", "s", data_model="doc", kind="NOTUNIQUE_HASH_INDEX")),
    )
    # O16: a Long that wraps on an INTEGER index, a double that rounds on a FLOAT index
    assert o16_predicate(schema, "n1", Predicate("source", "i", "=", "p", 2**32 + 5), "rel") and not o16_predicate(schema, "n1", Predicate("source", "i", "=", "p", 5), "rel")
    assert o16_predicate(schema, "n1", Predicate("source", "f", "=", "p", 0.1), "doc") and not o16_predicate(schema, "n1", Predicate("source", "f", "<", "p", 0.1), "doc")  # a hash index scans a range
    assert not o16_predicate(schema, "n1", Predicate("source", "f", "=", "p", 0.125), "doc")
    # O17: a wrapping bound on an INTEGER column, a rounding one on a FLOAT column, everywhere
    assert o17_predicate(schema, "n1", Predicate("source", "i", "BETWEEN", "p", [1, 2**31]))
    assert o17_predicate(schema, "n1", Predicate("source", "f", "BETWEEN", "p", [0.1, 1.0])) and not o17_predicate(schema, "n1", Predicate("source", "f", "BETWEEN", "p", [0.125, 1.0]))
    assert not o17_predicate(schema, "n1", Predicate("source", "d", "BETWEEN", "p", [0.1, 1.0]))
    # O20: a non-dyadic double on an indexed DECIMAL column
    assert o20_predicate(schema, "n1", Predicate("source", "d", "=", "p", 0.1), "graph") and not o20_predicate(schema, "n1", Predicate("source", "d", "=", "p", 0.1), "doc")
    assert not o20_predicate(schema, "n1", Predicate("source", "d", "=", "p", 0.5), "graph")
    # O21: IN / BETWEEN on an indexed ci column (a hash index serves IN only)
    assert o21_predicate(schema, "n1", Predicate("source", "s", "IN", "p", ["a"]), "rel") and o21_predicate(schema, "n1", Predicate("source", "s", "IN", "p", ["a"]), "doc")
    assert o21_predicate(schema, "n1", Predicate("source", "s", "BETWEEN", "p", ["a", "b"]), "rel") and not o21_predicate(schema, "n1", Predicate("source", "s", "BETWEEN", "p", ["a", "b"]), "doc")
    assert not o21_predicate(schema, "n1", Predicate("source", "s", "=", "p", "a"), "rel")
    assert node_case_known("n1", Predicate("source", "s", "IN", "p", ["a"]), schema, inst) == {"doc": "O21", "rel": "O21"}
    # O11: `<` on the second field of a composite index next to an equality on its lead
    inst2 = build(3, GeneratorConfig())[1]
    comp = Schema(node_types=(NodeTypeSpec("n1", (FieldSpec("f1", FieldType.INT), FieldSpec("f2", FieldType.INT))),), edge_types=(), indexes=(IndexSpec("n1", "f1", extra_fields=("f2",), data_model="both", kind="NOTUNIQUE"),))
    from ganda.core.data.model import NodeRow

    rows = Instance(comp, [NodeRow("n1", 0, {"f1": 1}), NodeRow("n1", 1, {"f1": 1, "f2": 0})], [])
    both = Compound("source", "AND", (Predicate("source", "f1", "=", "p_0", 1), Predicate("source", "f2", "<", "p_1", 1)))
    assert o11_predicate(comp, rows, "n1", both, "doc")
    assert not o11_predicate(comp, rows, "n1", Predicate("source", "f2", "<", "p", 1), "doc")
    # O18 / O23 for an ordered-keys shape
    assert ordered_keys_known(comp, rows, "n1", ["f1", "f2"], ["ASC", "ASC"], "doc") == "O18"
    assert ordered_keys_known(comp, rows, "n1", ["f1"], ["DESC"], "doc") == "O18"
    assert ordered_keys_known(comp, rows, "n1", ["f1", "f2"], ["ASC", "DESC"], "doc") is None
    keep = Schema(node_types=comp.node_types, edge_types=(), indexes=(IndexSpec("n1", "f1", extra_fields=("f2",), data_model="both", ignore_nulls=False, kind="NOTUNIQUE"),))
    assert ordered_keys_known(keep, rows, "n1", ["f1", "f2"], ["ASC", "ASC"], "doc") == "O23"
    assert ordered_keys_known(keep, rows, "n1", ["f2"], ["ASC"], "doc") is None
    single = Schema(node_types=comp.node_types, edge_types=(), indexes=(IndexSpec("n1", "f1", data_model="both", ignore_nulls=False, kind="NOTUNIQUE"),))
    assert ordered_keys_known(single, rows, "n1", ["f1"], ["ASC"], "doc") is None


def test_o25_withholds_bracket_syntaxes_on_ci_targets():
    from ganda.orientdb.known_bugs.guards import o25_shape

    schema = Schema(
        node_types=(NodeTypeSpec("n1", (FieldSpec("s", FieldType.STRING, case_insensitive=True),)),),
        edge_types=(EdgeTypeSpec("e1", "n1", "n1", ()),),
    )
    tp = Predicate("target", "s", "=", "p1", "a", "string", True)
    q = TraversalQuery("n1", (Hop("e1", "out", tp),), "ids")
    assert o25_shape(q, schema, "graph", "chain") and o25_shape(q, schema, "doc", "path") and o25_shape(q, schema, "graph", "edgescan")
    assert not o25_shape(q, schema, "graph", "match") and not o25_shape(q, schema, "rel", "let") and not o25_shape(q, schema, "doc", "inpath")
    sp = Predicate("source", "s", "=", "p0", "a", "string", True)
    q2 = TraversalQuery("n1", (Hop("e1", "out"),), "ids", None, sp)
    assert o25_shape(q2, schema, "graph", "edgescan") and not o25_shape(q2, schema, "graph", "chain")
    assert not o25_shape(TraversalQuery("n1", (Hop("e1", "out", Predicate("target", "s", "IN", "p1", ["a"], "string", True)),), "ids"), schema, "graph", "chain")


def test_o36_between_on_a_composite_lead_next_to_a_later_field():
    from ganda.orientdb.known_bugs.guards import o36_predicate
    from ganda.orientdb.facade.predicates import Compound

    schema = Schema(
        node_types=(NodeTypeSpec("n1", (FieldSpec("f", FieldType.INT), FieldSpec("g", FieldType.INT), FieldSpec("h", FieldType.INT))),),
        edge_types=(),
        indexes=(IndexSpec("n1", "f", data_model="rel", kind="NOTUNIQUE", extra_fields=("g",)), IndexSpec("n1", "h", data_model="graph", kind="NOTUNIQUE_HASH_INDEX", extra_fields=("g",))),
    )
    between = Predicate("source", "f", "BETWEEN", "p", [1, 3], ftype="int")
    later = Predicate("source", "g", ">=", "q", 20, ftype="int")
    both = Compound("source", "AND", (later, between))  # either conjunct order
    assert o36_predicate(schema, "n1", both, "rel")
    assert not o36_predicate(schema, "n1", both, "doc")  # no composite there
    assert not o36_predicate(schema, "n1", between, "rel")  # the lead alone
    assert not o36_predicate(schema, "n1", Compound("source", "OR", (between, later)), "rel")  # not conjuncts
    ge = Compound("source", "AND", (Predicate("source", "f", ">=", "a", 1, ftype="int"), Predicate("source", "f", "<=", "b", 3, ftype="int"), later))
    assert not o36_predicate(schema, "n1", ge, "rel")  # the same range rendered >= / <=
    hb = Compound("source", "AND", (Predicate("source", "h", "BETWEEN", "p", [1, 3], ftype="int"), later))
    assert not o36_predicate(schema, "n1", hb, "graph")  # a hash composite does not serve the range
