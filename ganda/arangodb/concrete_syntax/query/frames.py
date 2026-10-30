from __future__ import annotations

from dataclasses import dataclass

ALL = ("doc", "graph", "rel")
# AQL binds every frame position to a variable, so a cell's scope is the expression a
# predicate's names hang off: a loop variable (`var`), a traversal's path (`path`,
# `dp.vertices[k]`), an inline operator's element (`deref`, `DOCUMENT(CURRENT.target)`)


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
    target: str = "var"


_EDGES = "the traversal operator and the path functions walk edge collections"
_LINKS = "only doc stores a relationship inside its source document"

FRAMES = {f.name: f for f in (
    Frame("collection", "traversal"),
    Frame("rows", "traversal"),
    Frame("reach_union", "reach"),
    Frame("reach_traverse", "reach", ("graph",), _EDGES),
    Frame("path_traverse", "path", ("graph",), _EDGES),
    Frame("path_levels", "path"),
    Frame("shortest_path", "path", ("graph",), _EDGES),
    Frame("select_nodes", "select"),
    Frame("select_edges", "select"),
    Frame("node_update", "write"),
    Frame("node_delete", "write"),
    Frame("edge_update", "write", ("graph", "rel"), "doc has no relationship documents: its write rewrites the owner's array"),
    Frame("edge_delete", "write", ("graph", "rel"), "doc has no relationship documents: its write rewrites the owner's array"),
    Frame("owner_rewrite", "write", ("doc",), _LINKS),
)}

TRAVERSAL = (
    Cell("collection", "graph", "trav"),
    Cell("collection", "graph", "escan"),
    Cell("collection", "graph", "deep", "path"),
    Cell("collection", "doc", "links"),
    Cell("collection", "doc", "join"),
    Cell("collection", "doc", "inline", "deref"),
    Cell("collection", "rel", "join"),
    Cell("collection", "rel", "sub"),
    Cell("rows", "graph", "flat"),
    Cell("rows", "doc", "flat"),
    Cell("rows", "rel", "flat"),
)

# a reach question has no predicate on its hop
REACH = (
    Cell("reach_traverse", "graph", "trav"),
    Cell("reach_union", "doc", "links"),
    Cell("reach_union", "rel", "join"),
)

PATH = (
    Cell("shortest_path", "graph", ""),
    Cell("shortest_path", "graph", "ksp"),
    Cell("shortest_path", "graph", "asp"),
    Cell("path_traverse", "graph", "bfs"),
    Cell("path_traverse", "graph", "walk"),
    Cell("path_levels", "doc", ""),
    Cell("path_levels", "doc", "join"),
    Cell("path_levels", "rel", ""),
    Cell("path_levels", "rel", "deref"),
)

SELECT = (
    Cell("select_nodes", "doc", "node"),
    Cell("select_nodes", "graph", "node"),
    Cell("select_nodes", "rel", "node"),
    Cell("select_edges", "graph", "edge"),
    Cell("select_edges", "rel", "edge"),
    Cell("select_edges", "doc", "links"),
)

WRITE = (
    Cell("node_update", "doc", "update"),
    Cell("node_update", "doc", "replace"),
    Cell("node_update", "graph", "update"),
    Cell("node_update", "graph", "replace"),
    Cell("node_update", "rel", "update"),
    Cell("node_update", "rel", "replace"),
    Cell("node_update", "doc", ""),
    Cell("node_update", "graph", ""),
    Cell("node_update", "rel", ""),
    Cell("node_delete", "doc", "deref"),
    Cell("node_delete", "graph", ""),
    Cell("node_delete", "rel", ""),
    Cell("edge_update", "graph", "update"),
    Cell("edge_update", "graph", "replace"),
    Cell("edge_update", "rel", "update"),
    Cell("edge_update", "rel", "replace"),
    Cell("edge_delete", "graph", ""),
    Cell("edge_delete", "rel", ""),
    Cell("owner_rewrite", "doc", "bracket"),
    Cell("owner_rewrite", "doc", "inline"),
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
