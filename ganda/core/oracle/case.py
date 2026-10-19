"""Shared types for the query layer.

A `QueryCase` is one concrete instantiation of a metamorphic relation: one query text
per data model that a correct engine must answer identically once results are
normalized per `comparison`. The keys of `queries` are VARIANTS: a data model of the
engine (`doc`, `graph`, `rel`) or `<data model>:<tag>` -- a second concrete syntax of
the same question run in that data model's database (`graph:match`, `rel:flat`), so
the oracle's every-pair loop gives both the same-database pair (`graph/graph:match`:
two engine paths over one storage layout) and the cross-model pair.

Values travel in `bind_vars`, shared by every variant; how they reach the engine (bind
parameters, literals) is the provider session's business.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from enum import Enum


class Comparison(Enum):
    # {id: value} maps must match exactly; a data model returning one id twice is a finding
    SCALAR_BY_ID = "scalar_by_id"
    # {id: [values...]} as sorted bags, nested lists flattened, nulls dropped; a data model
    # may answer with several rows per id (MATCH, a junction scan) -- they are folded
    MULTISET_BY_ID = "multiset_by_id"
    # like MULTISET_BY_ID but deduplicated
    SET_BY_ID = "set_by_id"
    # the whole result is one bare value (a count, an ordered id list)
    SINGLE_VALUE = "single_value"
    # one bare value compared modulo list order at every depth (a set of ids)
    NESTED_VALUE = "nested_value"


from ganda.core.data.model import DATA_MODELS


@dataclass(frozen=True)
class QueryCase:
    generator_name: str
    edge_type: str  # the relationship the case is about ("" for node-only cases)
    comparison: Comparison
    # variant (a data model or `<data model>:<tag>`) -> query text. Every data model has
    # an entry unless `params["partial"]` (a question with no concrete syntax on some
    # models).
    queries: dict
    bind_vars: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)  # human-readable, for repro reporting
    result_field: str = "v"
    # the structural signature of the question (the census and deduplication key)
    shape: str = ""
    # variant -> known-bug id: the variant's text is the exact shape of a bug already
    # filed, so the campaign withholds it and counts the suppression instead
    known_bugs: dict = field(default_factory=dict)
    # variant -> engine options the session passes through with the text (a plan hint)
    variant_options: dict = field(default_factory=dict)
    # the provider the texts are written for (None: the standard three data models and
    # the core's default value order)
    engine: Any = field(default=None, compare=False, repr=False)

    @property
    def data_models(self) -> tuple:
        return self.engine.data_models if self.engine is not None else DATA_MODELS

    def __post_init__(self):
        for variant in self.queries:
            base, sep, tag = variant.partition(":")
            if base not in self.data_models or (sep and not tag):
                raise ValueError(f"variant {variant!r} must be a data model or <data model>:<tag>")
        if self.params.get("partial"):
            # a question only some storage models can render (a graph function):
            # the pairs run among the data models present, plus the reference leg
            if not any(s in self.queries for s in self.data_models):
                raise ValueError("a partial case needs at least one data model")
            return
        for s in self.data_models:
            if s not in self.queries:
                raise ValueError(f"missing data model {s!r}")

    @property
    def variants(self) -> list:
        return sorted(self.queries, key=lambda s: (self.data_models.index(s.split(":", 1)[0]), s))

    @property
    def tagged_variants(self) -> list:
        return [s for s in self.variants if ":" in s]

    def query_for(self, variant: str) -> str:
        return self.queries[variant]

    def options_for(self, variant: str) -> dict:
        return self.variant_options.get(variant, {})


def data_model_of(variant: str) -> str:
    return variant.split(":", 1)[0]
