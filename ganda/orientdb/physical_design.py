"""OrientDB's physical design: what an index of each kind enforces."""
from __future__ import annotations

from ganda.orientdb.dialect import UNIQUE_KINDS


def is_unique(ix) -> bool:
    """A second record with the same key is REJECTED."""
    return ix.kind in UNIQUE_KINDS


def holds_one_record_per_key(ix) -> bool:
    """One record per key, by rejection (unique) or by replacement (DICTIONARY)."""
    return ix.kind in UNIQUE_KINDS + ("DICTIONARY",)
