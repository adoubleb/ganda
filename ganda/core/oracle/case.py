"""
A `QueryCase` is one concrete instantiation of a metamorphic relation: one query text
per data model that a correct engine must answer identically once results are
normalized per `comparison`. 
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from enum import Enum


class Comparison(Enum):
    SCALAR_BY_ID = "scalar_by_id"
    MULTISET_BY_ID = "multiset_by_id"
    SET_BY_ID = "set_by_id"
    SINGLE_VALUE = "single_value"
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
    # variant -> its spelling at the IR level (a rewritten filter, a form: the same choice
    # on every data model); absent: the plain spelling. The oracle pairs a variant with
    # the same spelling on another data model, whatever the data model's own syntax
    # (`graph:match` / `doc:path`), never with another variant of its own data model
    spellings: dict = field(default_factory=dict)
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
            # the pairs run among the data models present
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


def comparable_pairs(case: QueryCase, variants: list) -> list:
    """The pairs of `variants` the oracle compares: two DATA MODELS asked in one spelling.
    Ganda's question is the multi-model relation, so two syntaxes of one data model are
    never compared, nor two spellings."""
    import itertools

    return [(a, b) for a, b in itertools.combinations(variants, 2) if data_model_of(a) != data_model_of(b) and case.spellings.get(a, "") == case.spellings.get(b, "")]
