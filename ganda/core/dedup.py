"""Finding deduplication (the G2 rule): bug frequency is a power law, so one defect
re-found on every instance would bury a second one. A finding is keyed by
`(template, shape, failing pairs, class)`; the first occurrence of a key is bundled,
later ones are counted. The key deliberately keeps the *pair set* and the *shape* so a
new pairing or a new structural shape of a known key still gets its own bundle.
"""
from __future__ import annotations

import threading
from collections import Counter


class BugDeduplicator:
    def __init__(self):
        self._seen: Counter = Counter()
        self._lock = threading.Lock()

    @staticmethod
    def key(result) -> tuple:
        cls = result.finding_class or "ok"
        pairs = tuple(result.failing_pairs)
        if cls == "reference_mismatch":
            pairs = tuple(sorted({m.variant for m in result.reference_mismatches}))
        elif cls == "error_asymmetry":
            pairs = tuple(sorted(s for s in result.variants if result.errors.get(s)))
        elif cls == "internal_error":
            pairs = tuple(sorted(s for s, i in result.internal.items() if i))
        elif cls == "duplicate_id_rows":
            pairs = tuple(sorted({d.variant for d in result.duplicate_id_rows}))
        return (result.case.generator_name, result.case.shape, pairs, cls)

    def admit(self, result) -> "tuple[bool, int]":
        """(first time seen, times seen so far including this one)."""
        k = self.key(result)
        with self._lock:
            self._seen[k] += 1
            return self._seen[k] == 1, self._seen[k]

    def summary(self) -> dict:
        with self._lock:
            return {" | ".join(str(x) for x in k): v for k, v in sorted(self._seen.items(), key=lambda kv: -kv[1])}
