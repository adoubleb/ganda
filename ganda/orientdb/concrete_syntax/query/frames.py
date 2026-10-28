"""The frame grid: every concrete syntax of the four IRs as a frame, the data model that
fills it and the scope its names take there. A FRAME is the text every data model
shares (the projection, the kind's aggregate, the door, the depth bound); its SLOT is
the model-specific part, how a data model stores and reads a relationship (a graph
function, an embedded link list, a junction class); the SCOPE is what the IR's names --
the source, a hop's target and edge, a select's or a write's record -- are at each
position of the frame (`scope.py`).

    IR         frame             shared text (the slot in <>)                  filled by
    traversal  collection        SELECT id, P(<C>) AS v FROM src [LET] WHERE   graph doc rel
                                 sp [AND <C>.size() ...]
               rows              one (id, v) row per path from <R>, folded     graph doc rel
                                 by id
    reach      reach_union       unionall([@this], <L1>, .., <Lk>).asSet()     graph doc rel
               reach_traverse    TRAVERSE <walk> FROM $parent.$current         graph
                                 MAXDEPTH k STRATEGY s
               reach_match       MATCH .out(e){while: ($depth < k)}            graph
    path       path_traverse     min($depth) + 1 FROM (TRAVERSE <walk> FROM    graph doc
                                 start MAXDEPTH k BREADTH_FIRST) WHERE id = :b
               path_levels       min(d) + 1 over the union of the levels       rel
                                 reaching :b
               shortest_path     shortestPath(a, b, dir, e[, maxDepth])        graph
               path_match        MATCH .out(e){maxDepth | while, depthAlias}   graph
    select     select_nodes      the kind x form over FROM t (no slot: a node  graph doc rel
                                 is stored alike on every data model)
               select_edges      the kind x form over FROM <records>           graph rel
               select_links      the link ids unwound from FROM src            doc
    write      node_update       UPDATE t <change> [RETURN] WHERE p            graph doc rel
               node_delete       <verb> t WHERE p, the cascade around it       graph doc rel
               edge_update       <verb> <records> <change> WHERE p             graph rel
               edge_delete       <verb> <records> WHERE p                      graph rel
               owner_rewrite     UPDATE src SET e = <rewritten list>           doc

A cell's `target` says how its hop's target (and edge) predicates are scoped: `record`
(the subquery's or the alias's own record), `bracket` (a bracket filter over a
collection's elements), `dotted` (an edge's end, `in.f`), `lookup` (a LET per row,
`$n.size() > 0`), `semijoin` (`far IN (SELECT id ...)`). `correlated`: the slot is a
subquery read through `$parent.$current`. The guards read both (`CORRELATED`, `BRACKET`:
O1 and O25).

A frame's `fillable` is the data models that could fill it; one of them without a cell is
a HOLE (`holes()`), a question the data models could be compared on and are not. The
others are conceded, `why` says on what ground.
"""
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

# the traversal syntaxes whose slot is a correlated subquery (O1 reads them) and those
# that put a target predicate in a bracket filter or behind a dotted link (O25)
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
