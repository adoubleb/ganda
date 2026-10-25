"""F28's guard, the known-bug marks on generated cases, and the mutants."""
from ganda.core.data.model import FieldType, IndexSpec
from ganda.core.query.ir.predicates import Compound, Predicate
from ganda.surrealdb import PROVIDER, Schema
from ganda.surrealdb.known_bugs import select_known, write_known
from ganda.surrealdb.known_bugs.predicate import f28_predicate, node_case_known
from ganda.surrealdb.mutation.mutants import MUTANTS, MUTANTS_BY_NAME
from tests.surrealdb.helpers import EVERY_AXIS, GeneratorConfig, build


def _schema(index_data_model="doc"):
    from ganda.core.data.model import EdgeTypeSpec, FieldSpec, NodeTypeSpec

    n1 = NodeTypeSpec("n1", (FieldSpec("f1", FieldType.INT), FieldSpec("f3", FieldType.LIST)))
    ix = IndexSpec(table="n1", field="f3", data_model=index_data_model, kind="standard")
    return Schema((n1,), (EdgeTypeSpec("e1", "n1", "n1", ()),), (ix,))


def test_f28_marks_the_data_model_whose_index_serves_a_whole_array_equality():
    schema = _schema("doc")
    eq = Predicate("source", "f3", "=", "p", [1], "list")
    rev = Predicate("source", "f3", "=", "p", [1], "list", rev=True)
    inside = Predicate("source", "f3", "IN", "p", [[1], [2]], "list")
    has = Predicate("source", "f3", "HAS", "p", 1, "list")
    assert node_case_known("n1", eq, schema, None) == {"doc": "F28"}
    assert node_case_known("n1", rev, schema, None) == {"doc": "F28"}
    assert node_case_known("n1", Compound("source", "OR", (has, inside)), schema, None) == {"doc": "F28"}
    assert node_case_known("n1", has, schema, None) == {}
    assert node_case_known("n1", Predicate("source", "f3", "<>", "p", [1], "list"), schema, None) == {}
    both = _schema("both")
    assert node_case_known("n1", eq, both, None) == {"doc": "F28", "graph": "F28", "rel": "F28"}
    assert not f28_predicate(_schema("graph"), "n1", eq, "doc")


def test_generated_cases_carry_the_f28_mark_and_only_it():
    marks = set()
    for seed in range(1, 8):
        cfg = GeneratorConfig(**{**EVERY_AXIS, "list_index_probability": 1.0, "index_probability": 0.8})
        schema, inst, rng = build(seed, cfg)
        from ganda.core.query.generators.registry import all_cases

        for c in all_cases(rng, schema, inst, cfg=cfg):
            marks |= set(c.known_bugs.values())
    assert marks <= {"F28"} and marks


def test_select_and_write_guards_read_the_filter():
    from ganda.core.query.ir.select import SelectQuery
    from ganda.core.query.ir.write import WriteQuery
    from ganda.surrealdb.concrete_syntax.query.select import BASE, Rewrites
    from ganda.surrealdb.concrete_syntax.query.write import WriteRewrites, WriteSyntax

    schema = _schema("rel")
    p = Predicate("source", "f3", "=", "p", [], "list")
    assert select_known(SelectQuery("n1", p, "ids"), BASE, Rewrites(), "rel", schema, None) == "F28"
    assert select_known(SelectQuery("n1", p, "ids"), BASE, Rewrites(), "doc", schema, None) is None
    assert write_known(WriteQuery("set", table="n1", where=p, field="f1", value=1), WriteSyntax(), WriteRewrites(), "rel", schema, None) == "F28"


def test_mutants_corrupt_something():
    assert len({m.name for m in MUTANTS}) == len(MUTANTS)
    from ganda.core.oracle.case import QueryCase

    case = QueryCase("ir_select", "", __import__("ganda.core.oracle.case", fromlist=["Comparison"]).Comparison.NESTED_VALUE, {"doc": "SELECT id FROM n1 WHERE f1 > $p", "graph": "SELECT id FROM n1 WHERE f1 > $p", "rel": "SELECT id FROM n1 WHERE f1 > $p"}, {"p": 1}, {"table": "n1", "select": {"edge": False}}, engine=PROVIDER)
    flipped = MUTANTS_BY_NAME["graph_wrong_predicate_op"].rewrite_case(case)
    assert flipped.queries["graph"] == "SELECT id FROM n1 WHERE f1 >= $p" and flipped.queries["doc"] == case.queries["doc"]
    from ganda.core.data.model import EdgeTypeSpec, FieldSpec, NodeTypeSpec

    schema = Schema((NodeTypeSpec("n1", (FieldSpec("f1", FieldType.INT),)),), (), (IndexSpec(table="n1", field="f1", data_model="rel", kind="standard"),))
    nulls = MUTANTS_BY_NAME["rel_index_null_keys"].rewrite_case_schema(case, schema)
    assert nulls.queries["rel"] == "SELECT id FROM n1 WHERE (f1 > $p OR f1 IS NONE)" and nulls.queries["graph"] == case.queries["graph"]
    assert MUTANTS_BY_NAME["graph_index_range_inclusive"].rewrite_case_schema(case, schema) == case  # no graph index
    doc = QueryCase("ir_traversal", "e1", case.comparison, {"doc": "SELECT id, (e1 ?? [])[WHERE w > $p].target AS v FROM n1", "graph": "x", "rel": "x"}, {}, {}, engine=PROVIDER)
    assert MUTANTS_BY_NAME["doc_distinct_projection"].rewrite_case(doc).queries["doc"] == "SELECT id, array::distinct((e1 ?? [])[WHERE w > $p].target) AS v FROM n1"
