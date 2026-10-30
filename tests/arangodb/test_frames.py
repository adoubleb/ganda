"""The frame grid (`concrete_syntax/query/frames.py`) in step with the renderers'
catalogs: every concrete syntax of the four IRs sits in exactly one cell, every cell
names one the renderers have, and the holes are the ones recorded."""
from collections import Counter
from types import SimpleNamespace

from ganda.core.data.model import EdgeRow, EdgeTypeSpec, Instance, NodeRow, NodeTypeSpec
from ganda.core.query.ir.path import PathQuery
from ganda.core.query.ir.write import EDGE_OPS, NODE_OPS
from ganda.arangodb import Schema
from ganda.arangodb.concrete_syntax.query import frames, path, select, traversal, write
from ganda.arangodb.concrete_syntax.query.frames import CELLS, FRAMES, PATH, REACH, SELECT, TRAVERSAL, WRITE


def _keys(cells) -> set:
    return {(c.data_model, c.syntax) for c in cells}


def test_cells_are_unique_and_fillable():
    assert not [k for k, n in Counter((c.frame, c.data_model, c.syntax) for c in CELLS).items() if n > 1]
    for c in CELLS:
        assert c.data_model in FRAMES[c.frame].fillable, c
    for name, f in FRAMES.items():
        assert any(c.frame == name for c in CELLS), name
        assert (f.fillable == frames.ALL) or f.why, name


def test_traversal_cells_are_the_syntaxes():
    assert _keys(TRAVERSAL) == {(dm, s) for dm, ss in traversal.SYNTAXES.items() for s in ss}
    assert len(_keys(TRAVERSAL)) == len(TRAVERSAL)
    assert _keys(REACH) == set(traversal.BASE_SYNTAX.items())


def test_path_cells_are_the_variants():
    schema = Schema(node_types=(NodeTypeSpec("n1", ()),), edge_types=(EdgeTypeSpec("e1", "n1", "n1", ()),))
    inst = Instance(schema, [NodeRow("n1", i, {}) for i in range(4)], [EdgeRow("e1", i, f"n1/r{i}", f"n1/r{i + 1}", {}) for i in range(3)])
    q = PathQuery("n1/r0", "n1", "n1/r3", "n1", "OUT", "e1", 3)
    tags = {tuple(v.split(":")) if ":" in v else (v, "") for v in path.render_all(q, schema, inst)[0]}
    assert tags == _keys(PATH)


def test_select_cells_are_the_targets():
    for edge in (False, True):
        for dm in select.base_data_models(SimpleNamespace(edge=edge)):
            frame = "select_edges" if edge else "select_nodes"
            assert any(c.frame == frame and c.data_model == dm for c in SELECT), (edge, dm)
    assert {c.data_model for c in SELECT if c.frame == "select_edges"} == set(frames.ALL)


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
    assert {(c.data_model, c.syntax) for c in WRITE} == {(dm, f) for op in NODE_OPS + EDGE_OPS for dm in frames.ALL for f in write.forms(SimpleNamespace(op=op), dm)}


def test_holes():
    # graph could walk its edge collection level by level, as doc and rel do
    assert frames.holes() == {"reach_union": ("graph",), "path_levels": ("graph",)}
