from __future__ import annotations

from dataclasses import dataclass

ALL = ("doc", "graph", "rel")


@dataclass(frozen=True)
class Frame:
    name: str
    ir: str
    fillable: tuple = ALL
    why: str = ""


@dataclass(frozen=True)
class Cell:
    frame: str
    data_model: str
    # the concrete syntax's name in its IR's catalog: a traversal syntax, a path
    # variant's tag ("" the base text), a select target, a write form ("" the plain one)
    syntax: str
    # how a target predicate's names resolve: on the record, in a bracket filter, through
    # a dotted link (`out.f`, `dst.f`) or a closure's parameter (`$r.f`)
    target: str = "record"


FRAMES = {f.name: f for f in (
    Frame("collection", "traversal"),
    Frame("rows", "traversal"),
    Frame("reach_union", "reach"),
    Frame("reach_traverse", "reach", ("doc", "graph"), "the junction is reached by a subquery, not a link the recursive idiom follows"),
    Frame("shortest_path", "path", ("doc", "graph"), "the junction is reached by a subquery, not a link the recursive idiom follows"),
    Frame("path_levels", "path"),
    Frame("select_nodes", "select"),
    Frame("select_edges", "select"),
    Frame("select_links", "select", ("doc",), "only doc stores a relationship inside its source record"),
    Frame("node_update", "write"),
    Frame("node_delete", "write"),
    Frame("edge_update", "write", ("graph", "rel"), "doc has no relationship records: its write rewrites the owner's list"),
    Frame("edge_delete", "write", ("graph", "rel"), "doc has no relationship records: its write rewrites the owner's list"),
    Frame("owner_rewrite", "write", ("doc",), "only doc stores a relationship inside its source record"),
)}

TRAVERSAL = (
    Cell("collection", "graph", "arrow", "bracket"),
    Cell("collection", "graph", "subquery", "dotted"),
    Cell("collection", "doc", "links", "closure"),
    Cell("collection", "doc", "closure", "closure"),
    Cell("collection", "rel", "subquery", "dotted"),
    Cell("rows", "graph", "edgescan", "dotted"),
    Cell("rows", "rel", "flat", "dotted"),
)

# a reach question has no predicate on its hop
REACH = (
    Cell("reach_union", "graph", "arrow"),
    Cell("reach_union", "doc", "links"),
    Cell("reach_union", "rel", "subquery"),
    Cell("reach_traverse", "graph", "recursion"),
)

PATH = (
    Cell("shortest_path", "graph", ""),
    Cell("path_levels", "graph", "levels"),
    Cell("path_levels", "doc", ""),
    Cell("path_levels", "rel", ""),
)

SELECT = (
    Cell("select_nodes", "doc", "node"),
    Cell("select_nodes", "graph", "node"),
    Cell("select_nodes", "rel", "node"),
    Cell("select_edges", "graph", "edge"),
    Cell("select_edges", "rel", "edge"),
    Cell("select_links", "doc", "edge", "bracket"),
)

WRITE = (
    Cell("node_update", "doc", ""),
    Cell("node_update", "graph", ""),
    Cell("node_update", "rel", ""),
    Cell("node_delete", "doc", ""),
    Cell("node_delete", "graph", ""),
    Cell("node_delete", "rel", ""),
    Cell("edge_update", "graph", ""),
    Cell("edge_update", "rel", ""),
    Cell("edge_delete", "graph", ""),
    Cell("edge_delete", "rel", ""),
    Cell("owner_rewrite", "doc", "closure"),
)

CELLS = TRAVERSAL + REACH + PATH + SELECT + WRITE


def holes() -> dict:
    """frame -> the data models that could fill it and have no cell there."""
    out = {}
    for f in FRAMES.values():
        missing = tuple(m for m in f.fillable if not any(c.frame == f.name and c.data_model == m for c in CELLS))
        if missing:
            out[f.name] = missing
    return out
