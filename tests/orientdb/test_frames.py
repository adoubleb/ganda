"""The frame grid (`concrete_syntax/query/frames.py`) in step with the renderers'
catalogs: every concrete syntax of the four IRs sits in exactly one cell, every cell
names one the renderers have, and the holes are the ones recorded."""
from collections import Counter
from types import SimpleNamespace

from ganda.core.data.model import EdgeRow, EdgeTypeSpec, Instance, NodeRow, NodeTypeSpec
from ganda.core.query.ir.path import PathQuery
from ganda.core.query.ir.write import EDGE_OPS, NODE_OPS
from ganda.orientdb import Schema
from ganda.orientdb.concrete_syntax.query import frames, path, select, traversal, write
from ganda.orientdb.concrete_syntax.query.frames import CELLS, FRAMES, PATH, REACH, SELECT, TRAVERSAL, WRITE


def _keys(cells) -> set:
    return {(c.data_model, c.syntax) for c in cells}


def test_cells_are_unique_and_fillable():
    assert not [k for k, n in Counter((c.frame, c.data_model, c.syntax) for c in CELLS).items() if n > 1]
    for c in CELLS:
        assert c.data_model in FRAMES[c.frame].fillable, c
    for name, f in FRAMES.items():
        assert any(c.frame == name for c in CELLS), name
        # a frame some data model cannot fill says why
        assert (f.fillable == frames.ALL) or f.why, name


def test_traversal_cells_are_the_renderers():
    assert _keys(TRAVERSAL) == set(traversal._RENDERERS)
    assert _keys(REACH) == {("graph", s) for s in traversal.GRAPH_REACH_SYNTAXES} | {("doc", "path"), ("doc", "traverse"), ("rel", "let")}
    for dm, syntaxes in traversal.SYNTAXES.items():
        assert {s for d, s in _keys(TRAVERSAL + REACH) if d == dm} == set(syntaxes), dm
    assert len(_keys(TRAVERSAL)) == len(TRAVERSAL)


def test_guard_sets_read_off_the_grid():
    assert frames.CORRELATED == {("doc", "let_expand"), ("doc", "inpath"), ("doc", "union"), ("rel", "let"), ("graph", "traverse"), ("graph", "traverse_dfs"), ("graph", "traverse_e"), ("doc", "traverse")}
    assert frames.BRACKET == {("graph", "chain"), ("graph", "bag"), ("graph", "edgescan"), ("doc", "path"), ("doc", "unwind"), ("doc", "let_expand"), ("doc", "union")}


def test_path_cells_are_the_variants():
    schema = Schema(node_types=(NodeTypeSpec("n1", ()),), edge_types=(EdgeTypeSpec("e1", "n1", "n1", ()),))
    inst = Instance(schema, [NodeRow("n1", i, {}) for i in range(4)], [EdgeRow("e1", i, f"n1/r{i}", f"n1/r{i + 1}", {}) for i in range(3)])
    q = PathQuery("n1/r0", "n1", "n1/r3", "n1", "OUT", "e1", 3)
    tags = lambda queries: {tuple(v.split(":")) if ":" in v else (v, "") for v in queries}  # noqa: E731
    assert tags(path.render_all(q, schema, inst)[0]) == _keys(c for c in PATH if not c.gated)
    assert tags(path.render_gated(q, schema)) == _keys(c for c in PATH if c.gated)


def test_select_cells_are_the_targets():
    for kind in ("ids", "count", "agg", "group", "page"):
        for edge in (False, True):
            q = SimpleNamespace(edge=edge, kind=kind)
            for dm in select.base_data_models(q):
                frame = "select_nodes" if not edge else "select_links" if dm == "doc" else "select_edges"
                assert any(c.frame == frame and c.data_model == dm for c in SELECT), (kind, edge, dm)


def test_write_cells_are_the_forms():
    for op in NODE_OPS + EDGE_OPS:
        q = SimpleNamespace(op=op)
        for dm in frames.ALL:
            for form in write.forms(q, dm):
                if op in EDGE_OPS and dm == "doc":
                    frame = "owner_rewrite"
                elif op in EDGE_OPS:
                    frame = "edge_delete" if op == "edge_delete" else "edge_update"
                else:
                    frame = "node_delete" if op == "delete" else "node_update"
                assert any(c.frame == frame and c.data_model == dm and c.syntax == form for c in WRITE), (op, dm, form)


def test_holes():
    assert frames.holes() == {}


def test_unwound_select_is_the_edge_frame_over_the_links():
    schema = Schema(node_types=(NodeTypeSpec("n1", ()),), edge_types=(EdgeTypeSpec("e1", "n1", "n1", ()),))
    q = SimpleNamespace(table="e1", where=None, kind="page", fields=("w",), field="w", directions=("DESC",), skip=1, limit=2, edge=True)
    assert select._kind_text(q, select.BASE, "FROM e1", select.EDGE) == "SELECT eid AS id FROM e1 ORDER BY w DESC, eid ASC SKIP 1 LIMIT 2"
    assert select.render_unwound(q, schema) == "SELECT l.eid AS id FROM (SELECT e1 AS l FROM n1 UNWIND l) WHERE l IS NOT NULL ORDER BY l.w DESC, l.eid ASC SKIP 1 LIMIT 2"


def test_gated_cells_are_asked_by_frame_holes_only():
    from tests.orientdb.helpers import GeneratorConfig, build
    from ganda.core.query.generators.registry import all_cases
    from ganda.orientdb.provider import frame_holes

    assert set(frame_holes.BUILDERS) == {c.frame for c in CELLS if c.gated}
    asked = Counter()
    for seed in range(1, 31):
        cfg = GeneratorConfig(frame_hole_cases=4)
        schema, instance, rng = build(seed, cfg)
        for case in all_cases(rng, schema, instance, cfg=cfg):
            gated = {v for v in case.queries if tuple(v.split(":")) in {(dm, sp) for _ir, dm, sp in frames.GATED}}
            if case.generator_name == frame_holes.GENERATOR:
                assert gated
                asked.update(gated)
            else:
                assert not gated, (case.generator_name, gated)
    assert set(asked) == {f"{dm}:{sp}" for _ir, dm, sp in frames.GATED}
    # off by default
    schema, instance, rng = build(1, GeneratorConfig())
    assert not [c for c in all_cases(rng, schema, instance, cfg=GeneratorConfig()) if c.generator_name == frame_holes.GENERATOR]
