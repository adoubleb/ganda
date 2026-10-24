"""The scale of an instance: how much data one seed materializes, and what the
campaign does differently when there is a lot of it.

`default` is the size every campaign so far ran at: a few classes of a few dozen short
rows, where the whole stored state is read back after the setup, and a bundle carries
the instance and every setup plan.

`large` is the storage-engine stress mode. The three data models of an engine differ
most in how they STORE a relationship and a record -- an embedded list, an edge bag, a
junction row -- and those paths are exercised by volume and by size: classes of
thousands of records with many fields, relationship types of thousands of edges with
hubs of thousands of links, records carrying strings, binaries and lists grown to the
engine's limits (`ganda.core.data.limits`), all written through bulk requests. The
stored state is compared by counts and a sample, and a bundle carries the shape of the instance and
the replay (`repro.py` regenerates the data from the seed).

A profile is a set of `GeneratorConfig` fields it changes WHERE THE FLAG WAS LEFT AT ITS
DEFAULT (an explicit flag wins), as a function of the engine's limits, plus the
campaign settings above. `resolve` applies it; `profile_of` answers the campaign's
questions.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ScaleProfile:
    name: str
    # 0: the whole stored state is read back; N: counts, plus N records per class and
    # N sources per relationship type (the heaviest ones first)
    state_sample: int
    # whether a bundle carries the instance's rows and the plans' statements in full
    embed_data: bool
    # a bundle's longest recorded value, in characters (0: whole values)
    max_bundle_value_chars: int
    # how long one query may run before it times out
    query_timeout_ms: int
    # whether a timed-out variant is read as a rejection (a finding when the others
    # answer: a plan that never finishes on a small instance) or left out of the
    # comparison (at scale a data model may simply be slower than the question allows)
    timeouts_are_errors: bool = True
    # GeneratorConfig fields <- the engine's limits
    overrides: "Callable[[object], dict] | None" = None


def _large(limits) -> dict:
    return dict(
        min_node_types=2,
        max_node_types=4,
        min_edge_types=2,
        max_edge_types=6,
        min_fields_per_type=6,
        max_fields_per_type=min(24, limits.max_fields_per_record),
        min_nodes_per_type=min(1000, limits.max_rows_per_table),
        max_nodes_per_type=min(5000, limits.max_rows_per_table),
        min_edges_per_type=2000,
        max_edges_per_type=20000,
        empty_table_probability=0.0,
        hub_probability=0.4,
        large_row_probability=0.003,
        binary_probability=0.3 if limits.max_binary_bytes else 0.0,
        statements_per_request=min(500, limits.max_statements_per_request),
        traversal_cases=10,
        select_cases=40,
        path_cases=2,
        write_cases=4,
    )


PROFILES = {
    "default": ScaleProfile("default", state_sample=0, embed_data=True, max_bundle_value_chars=0, query_timeout_ms=30000),
    # a minute per query: a relational hop is a correlated subquery, and three of them
    # over thousands of records is the performance cliff the mode is not about
    "large": ScaleProfile("large", state_sample=200, embed_data=False, max_bundle_value_chars=256, query_timeout_ms=60000, timeouts_are_errors=False, overrides=_large),


}


def profile(name: str) -> ScaleProfile:
    try:
        return PROFILES[name]
    except KeyError:
        raise ValueError(f"unknown scale {name!r}; known: {', '.join(PROFILES)}") from None


def profile_of(cfg) -> ScaleProfile:
    return profile(getattr(cfg, "scale", "default"))


def resolve(cfg, limits):
    """`cfg` with its scale profile applied: every field the profile names takes the
    profile's value where `cfg` still holds the field's default. Idempotent, so a
    bundle's recorded configuration resolves to itself."""
    p = profile_of(cfg)
    if p.overrides is None:
        return cfg
    defaults = {f.name: f.default for f in dataclasses.fields(type(cfg))}
    changes = {k: v for k, v in p.overrides(limits).items() if k in defaults and getattr(cfg, k) == defaults[k] and v != defaults[k]}
    return dataclasses.replace(cfg, **changes) if changes else cfg
