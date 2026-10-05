"""Shared types for the query layer.

A `QueryCase` is one concrete instantiation of a metamorphic relation: one OrientDB
SQL text per side that a correct engine must answer identically once results are
normalized per `comparison`. Sides are the three base representations (`doc`,
`graph`, `rel`) plus any number of *variant* sides named `<base>:<tag>` -- a second
spelling of the same question run in the base side's database (`graph:match`,
`rel:flat`, `doc:unwind`), so the oracle's every-pair loop gives both the same-database
pair (`graph/graph:match`: two engine paths over one storage layout) and the
cross-model pair through the variant.

Values never appear in query text: every pivot, start id or literal goes through
`bind_vars` (OrientDB named parameters, `:p`), shared by every side, so a hostile
generated string can never turn into a parse-error asymmetry that is really an
escaping bug in the fuzzer. The one exception is a class name or a field name, which
the generator synthesizes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Comparison(Enum):
    # {id: value} maps must match exactly; a side returning one id twice is a finding
    SCALAR_BY_ID = "scalar_by_id"
    # {id: [values...]} as sorted bags, nested lists flattened, nulls dropped; a side
    # may answer with several rows per id (MATCH, a junction scan) -- they are folded
    MULTISET_BY_ID = "multiset_by_id"
    # like MULTISET_BY_ID but deduplicated
    SET_BY_ID = "set_by_id"
    # the whole result is one bare value (a count, an ordered id list)
    SINGLE_VALUE = "single_value"
    # one bare value compared modulo list order at every depth (a set of ids)
    NESTED_VALUE = "nested_value"


SIDES = ("doc", "graph", "rel")


@dataclass(frozen=True)
class QueryCase:
    template_name: str
    edge_type: str  # the relationship the case is about ("" for node-only cases)
    comparison: Comparison
    # side (base or `<base>:<tag>`) -> SQL text. Every base side has an entry unless
    # `params["partial"]` (a question with no spelling on some models). A text that
    # starts with `BEGIN;` is a SCRIPT (`/command/<db>/script`: a transaction with
    # the answer in `RETURN $r`), run through the script door and unwrapped.
    queries: dict
    bind_vars: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)  # human-readable, for repro reporting
    result_field: str = "v"
    # the structural signature of the question (the census key); "" for hand templates
    shape: str = ""
    # side -> known-finding id: the side's spelling is the exact shape of a finding
    # already filed, so the runner withholds it and counts the suppression instead
    known: dict = field(default_factory=dict)

    def __post_init__(self):
        for side in self.queries:
            base, sep, tag = side.partition(":")
            if base not in SIDES or (sep and not tag):
                raise ValueError(f"side {side!r} must be a base side or <base>:<tag>")
        if self.params.get("partial"):
            # a question only some storage models can spell (a graph function):
            # the pairs run among the sides present, plus the reference leg
            if not any(s in self.queries for s in SIDES):
                raise ValueError("a partial case needs at least one base side")
            return
        for s in SIDES:
            if s not in self.queries:
                raise ValueError(f"missing base side {s!r}")

    @property
    def sides(self) -> list:
        return sorted(self.queries, key=lambda s: (SIDES.index(s.split(":", 1)[0]), s))

    @property
    def variant_sides(self) -> list:
        return [s for s in self.sides if ":" in s]

    def query_for(self, side: str) -> str:
        return self.queries[side]


def base_side(side: str) -> str:
    return side.split(":", 1)[0]
