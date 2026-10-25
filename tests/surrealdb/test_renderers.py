"""The four renderers over generated instances: every admitted syntax renders, the
variants and spellings are what the core expects, the texts carry the forms drawn."""
import random

from ganda.core.data.model import FieldType
from ganda.core.query.generators.registry import all_cases
from ganda.core.query.generators.write import sample_write
from ganda.core.query.generators.write_phase import build_write_case
from ganda.core.query.ir.predicates import Predicate
from ganda.core.query.ir.traversal import Hop, TraversalQuery
from ganda.core.query.ir.write import apply
from ganda.surrealdb import PROVIDER, Schema
from ganda.surrealdb.concrete_syntax.query import path, select, traversal, write
from tests.surrealdb.helpers import EVERY_AXIS, GeneratorConfig, build


def _schema():
    from ganda.core.data.model import EdgeTypeSpec, FieldSpec, NodeTypeSpec

    n1 = NodeTypeSpec("n1", (FieldSpec("f1", FieldType.INT), FieldSpec("f2", FieldType.STRING), FieldSpec("f3", FieldType.LIST), FieldSpec("f4", FieldType.DATETIME)))
    n2 = NodeTypeSpec("n2", (FieldSpec("f1", FieldType.INT),))
    e1 = EdgeTypeSpec("e1", "n1", "n2", (FieldSpec("w", FieldType.INT),))
    e2 = EdgeTypeSpec("e2", "n2", "n1", (FieldSpec("w", FieldType.FLOAT),))
    return Schema((n1, n2), (e1, e2))


def test_every_admitted_traversal_syntax_renders_on_every_data_model():
    schema = _schema()
    p1 = Predicate("edge", "w", ">", "p1", 1, "int")
    tp = Predicate("target", "f1", "<=", "p2", 5, "int")
    sp = Predicate("source", "f4", ">", "p0", "2024-01-01 00:00:00", "datetime", literal=True)
    questions = [
        TraversalQuery("n1", (Hop("e1", "out", p1),), "ids", None, sp),
        TraversalQuery("n1", (Hop("e1", "out", tp), Hop("e2", "out", p1)), "count"),
        TraversalQuery("n2", (Hop("e1", "in", p1),), "efield", "w"),
        TraversalQuery("n2", (Hop("e1", "in"), Hop("e1", "out", tp)), "distinct_ids"),
        TraversalQuery("n1", (Hop("e1", "out"),), "sum", "f1"),
        TraversalQuery("n1", (Hop("e1", "out", p1),), "nexists"),
        TraversalQuery("n1", (Hop("e1", "out"), Hop("e2", "out"), Hop("e1", "out")), "max", "f1"),
    ]
    for q in questions:
        for dm in PROVIDER.data_models:
            syntaxes = traversal.syntaxes_for(q, schema, dm)
            assert syntaxes, (q, dm)
            for syntax in syntaxes:
                text = traversal.render(q, schema, dm, syntax)
                assert text.startswith("SELECT ") and (" AS v FROM " in text or q.projection == "nexists"), (dm, syntax, text)
                if q.source_predicate is not None:
                    pulled = traversal.render(q, schema, dm, syntax, pull_up=True)
                    assert pulled.startswith("SELECT id") and ") WHERE " in pulled and 'd"2024-01-01T00:00:00Z"' in pulled
    # the undirected hop is two arrows, never `<->`
    both = TraversalQuery("n1", (Hop("e1", "both"),), "ids")
    assert "<->" not in traversal.render(both, _schema_self(), "graph", "arrow") and "array::concat(->e1->n1, <-e1<-n1)" in traversal.render(both, _schema_self(), "graph", "arrow")
    # a target predicate over record links goes through a closure
    assert "array::filter(" in traversal.render(questions[1], schema, "doc", "links")
    # a string / datetime aggregate is array::max, a sum math::sum
    assert "array::max(" in traversal.render(questions[6], schema, "graph", "arrow") and "math::sum(" in traversal.render(questions[4], schema, "rel", "subquery")


def _schema_self():
    from ganda.core.data.model import EdgeTypeSpec, FieldSpec, NodeTypeSpec

    n1 = NodeTypeSpec("n1", (FieldSpec("f1", FieldType.INT),))
    return Schema((n1,), (EdgeTypeSpec("e1", "n1", "n1", ()),))


def test_generated_cases_compare_data_models_only_and_render_every_variant():
    for seed in range(1, 5):
        cfg = GeneratorConfig(**EVERY_AXIS)
        schema, inst, rng = build(seed, cfg)
        cases = all_cases(rng, schema, inst, cfg=cfg)
        assert len(cases) > 100
        for c in cases:
            for v, text in c.queries.items():
                assert text and ("SELECT" in text or text.startswith("RETURN") or text.startswith("LET") or text.startswith("BEGIN")), (c.generator_name, v, text[:80])
            assert all(isinstance(name, str) for name in c.bind_vars)


def test_typed_constants_are_literals_in_every_ir():
    cfg = GeneratorConfig(**{**EVERY_AXIS, "date_map_probability": 1.0, "narrow_type_probability": 1.0})
    found = 0
    for seed in range(1, 6):
        schema, inst, rng = build(seed, cfg)
        for c in all_cases(rng, schema, inst, cfg=cfg):
            for v in c.bind_vars.values():
                assert not (isinstance(v, str) and len(v) == 19 and v[4] == "-" and v[10] == " "), (c.generator_name, c.shape, c.bind_vars)
            if any('d"' in t for t in c.queries.values()):
                found += 1
    assert found > 0


def test_select_forms_and_positions():
    schema, inst, rng = build(2, GeneratorConfig(**{**EVERY_AXIS, "inheritance_probability": 1.0}))
    from ganda.core.query.generators.select import sample_select

    seen = set()
    for _ in range(60):
        q = sample_select(rng, schema, inst, tx_probability=0.3)
        if q is None:
            continue
        rw = select.build_rewrites(q, rng, schema, inst)
        for dm in select.base_data_models(q):
            for sp in [select.BASE] + select.syntaxes_for(q, schema, rw, dm):
                text = select.render(q, schema, sp, rw, dm)
                seen.add((q.kind, sp.tag))
                if q.tx:
                    assert text.startswith("BEGIN TRANSACTION;") and "CANCEL TRANSACTION; RETURN $r" in text
                if sp.position == "poly":
                    assert "record::tb(id) = $cls" in text and select.extra_binds(q, schema, rw) == {"cls": q.table, "clsl": [q.table]}
                if q.kind == "group":
                    assert "?? NULL AS k" in text and "GROUP BY k" in text
    assert {k for k, _t in seen} >= {"ids", "count", "agg", "group", "page", "keys", "join", "distinct", "field"}
    assert any(t == "group_all" for _k, t in seen) and any(t == "poly" for _k, t in seen)


def test_path_answers_the_vertex_count_on_every_data_model():
    from ganda.core.query.ir.path import PathQuery

    schema = _schema_self()
    q = PathQuery("n1/r0", "n1", "n1/r3", "n1", "OUT", "e1", 3)
    queries, known = path.render_all(q, schema, None)
    assert set(queries) == {"graph", "graph:levels", "rel", "doc"} and known == {}
    assert "{..+shortest=n1:r3}(->e1->?)" in queries["graph"] and "array::len($p) > 3" in queries["graph"]
    assert queries["rel"].count("LET $l") == 4 and "THEN 4 ELSE 0 END" in queries["rel"]
    assert path.bind_vars(q) == {"a": "n1/r0", "b": "n1/r3", "depth": 3}
    assert "doc" not in path.render_all(PathQuery("n1/r0", "n1", "n1/r3", "n1", "BOTH", None, 2), schema, None)[0]


def test_writes_share_a_spelling_and_render_on_every_data_model():
    for seed in range(1, 6):
        schema, inst, rng = build(seed, GeneratorConfig(**EVERY_AXIS))
        wrng = random.Random(seed)
        for step in range(6):
            drawn = sample_write(wrng, schema, inst, step)
            if drawn is None:
                continue
            q, rw, syn = drawn
            assert len({write.spelling(sp) for sp, _k in syn.values()}) == 1
            after = apply(q, inst)
            case, statements = build_write_case(q, rw, syn, step, inst, after, False)
            assert set(statements) == set(PROVIDER.data_models)
            for dm, st in statements.items():
                text = st.script()
                assert "RETURN $r;" in text and ("{ count:" in text or "RETURN id," in text), (dm, text)
                if st.door == "tx":
                    assert text.startswith("BEGIN TRANSACTION;") and "COMMIT TRANSACTION;" in text
                if q.edge and dm == "doc":
                    assert "array::map(" in text or "array::filter(" in text
            inst = after
