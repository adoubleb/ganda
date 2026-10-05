"""Named, independently seeded random streams.

Every consumer of randomness draws from its own stream, so adding a query generator,
a schema axis or a provider hook never shifts the draws of another: a seed keeps
reproducing the same instance and the same cases.
"""
from __future__ import annotations

import random


def stream(seed: int, *parts) -> random.Random:
    """One named, independently-seeded RNG sub-stream. `random.Random` hashes a str
    seed with a fixed, un-salted algorithm, so this is deterministic across processes
    (unlike the builtin `hash()`, which must never be used here)."""
    return random.Random(f"{seed}:{':'.join(str(p) for p in parts)}")


class QueryRng(random.Random):
    """The rng handed to `all_cases()`; `for_generator(name)` gives each query
    generator its own independent sub-stream, so a query generator can be sampled,
    skipped or added without perturbing any other's draws, and a bundle's `repro.py`
    can reconstruct the exact case that failed."""

    def __init__(self, seed: int):
        super().__init__(f"{seed}:queries")
        self._seed = seed

    def for_generator(self, name: str) -> random.Random:
        return stream(self._seed, "queries", name)
