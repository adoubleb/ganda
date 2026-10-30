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
    target: str = "record"
    correlated: bool = False


FRAMES = {f.name: f for f in (
    Frame("collection", "traversal"),
    Frame("rows", "traversal"),
    Frame("reach_union", "reach"),
    Frame("reach_traverse", "reach", ("doc", "graph"), "the junction stores ids, not links: TRAVERSE has nothing to follow"),
    Frame("reach_match", "reach", ("graph",), "MATCH walks the graph's edges"),
    Frame("path_traverse", "path", ("doc", "graph"), "the junction stores ids, not links: TRAVERSE has nothing to follow"),
    Frame("path_levels", "path"),
    Frame("shortest_path", "path", ("graph",), "shortestPath() walks the graph's edges"),
    Frame("path_match", "path", ("graph",), "MATCH walks the graph's edges"),
    Frame("select_nodes", "select"),
    Frame("select_edges", "select", ALL),
    Frame("select_links", "select", ("doc",), "only doc stores a relationship inside its source record"),
    Frame("node_update", "write"),
    Frame("node_delete", "write"),
    Frame("edge_update", "write", ("graph", "rel"), "doc has no relationship records: its write rewrites the owner's list"),
    Frame("edge_delete", "write", ("graph", "rel"), "doc has no relationship records: its write rewrites the owner's list"),
    Frame("owner_rewrite", "write", ("doc",), "only doc stores a relationship inside its source record"),
)}

TRAVERSAL = (
    Cell("collection", "graph", "chain", "bracket"),
    Cell("collection", "graph", "bag", "bracket"),
    Cell("collection", "graph", "traverse", correlated=True),
    Cell("collection", "doc", "path", "bracket"),
    Cell("collection", "doc", "let_expand", "bracket", correlated=True),
    Cell("collection", "doc", "inpath", correlated=True),
    Cell("collection", "doc", "union", "bracket", correlated=True),
    Cell("collection", "rel", "let", "lookup", correlated=True),
    Cell("rows", "graph", "match"),
    Cell("rows", "graph", "match_opt"),
    Cell("rows", "graph", "edgescan", "dotted"),
    Cell("rows", "doc", "unwind", "bracket"),
    Cell("rows", "rel", "flat", "semijoin"),
)

# a reach question has no predicate on its hop
REACH = (
    Cell("reach_union", "graph", "chain"),
    Cell("reach_union", "doc", "path"),
    Cell("reach_union", "rel", "let", correlated=True),
    Cell("reach_traverse", "graph", "traverse", correlated=True),
    Cell("reach_traverse", "graph", "traverse_dfs", correlated=True),
    Cell("reach_traverse", "graph", "traverse_e", correlated=True),
    Cell("reach_traverse", "doc", "traverse", correlated=True),
    Cell("reach_match", "graph", "match"),
)

PATH = (
    Cell("path_traverse", "graph", "traverse"),
    Cell("path_traverse", "graph", "deep"),
    Cell("path_traverse", "doc", ""),
    Cell("path_traverse", "doc", "deep"),
    Cell("path_levels", "rel", ""),
    Cell("path_levels", "rel", "inline"),
    Cell("path_levels", "rel", "asc"),
    Cell("path_levels", "graph", "levels"),
    Cell("path_levels", "doc", "levels"),
    Cell("shortest_path", "graph", ""),
    Cell("shortest_path", "graph", "unbounded"),
    Cell("path_match", "graph", "match"),
    Cell("path_match", "graph", "while"),
)

SELECT = (
    Cell("select_nodes", "doc", "node"),
    Cell("select_nodes", "graph", "node"),
    Cell("select_nodes", "rel", "node"),
    Cell("select_edges", "graph", "edge"),
    Cell("select_edges", "rel", "edge"),
    Cell("select_edges", "doc", "unwound"),
    Cell("select_links", "doc", "edge", "bracket"),
)

WRITE = (
    Cell("node_update", "doc", ""),
    Cell("node_update", "graph", ""),
    Cell("node_update", "rel", ""),
    Cell("node_delete", "doc", ""),
    Cell("node_delete", "graph", ""),
    Cell("node_delete", "rel", ""),
    Cell("edge_update", "graph", "update_edge"),
    Cell("edge_update", "graph", "update"),
    Cell("edge_update", "rel", ""),
    Cell("edge_delete", "graph", ""),
    Cell("edge_delete", "rel", ""),
    Cell("owner_rewrite", "doc", "bracket"),
    Cell("owner_rewrite", "doc", "position"),
)

CELLS = TRAVERSAL + REACH + PATH + SELECT + WRITE

# the traversal syntaxes whose slot is a correlated subquery with `$parent.$current` in
# the outer query's scope (O1, when the outer query is index-served: `traverse` roots its
# TRAVERSE there, campaign c60b, seeds 414, 428 and 443) and those that put a target
# predicate in a bracket filter or behind a dotted link, where a `ci` collation is
# ignored (O25; `edgescan` renders the source predicate that way too)
CORRELATED = frozenset((c.data_model, c.syntax) for c in TRAVERSAL + REACH if c.correlated)
BRACKET = frozenset((c.data_model, c.syntax) for c in TRAVERSAL if c.target in ("bracket", "dotted"))


def holes() -> dict:
    """frame -> the data models that could fill it and have no cell there."""
    out = {}
    for f in FRAMES.values():
        missing = tuple(m for m in f.fillable if not any(c.frame == f.name and c.data_model == m for c in CELLS))
        if missing:
            out[f.name] = missing
    return out
