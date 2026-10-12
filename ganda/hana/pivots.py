"""SAP HANA's side of predicate sampling: the operators a leaf may draw and the pivots it
asks with.

The null tests are `IS NULL` / `IS NOT NULL` only: an absent field and a stored null
are one null on every data model (`dialect`). A pivot is always of the column's own
type: a parameter compared with a column takes the column's type in HANA SQL (an
integer column's `?` would truncate a `5.5`), while a SPARQL literal keeps its own, so a
cross-type pivot would ask the data models different questions.
"""
from __future__ import annotations

import random

ORDERED_OPS = ("=", "<>", ">", ">=", "<", "<=", "IN", "BETWEEN")
NULL_OPS = ("IS NULL", "IS NOT NULL")
# a LIKE pattern is built only from a value holding none of these
LIKE_WILDCARDS = "%_\\"


def ordered_ops(ftype) -> tuple:
    """Every comparison operator, on every ordered type: the reference models this
    engine's order inside each type."""
    return ORDERED_OPS


def cross_type(rng: random.Random, ftype, v):
    return v


def refine(rng: random.Random, ftype, v):
    return v
