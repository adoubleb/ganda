"""OrientDB's predicate operators: the ones a leaf may draw, and the characters a `LIKE`
pattern never holds."""
from __future__ import annotations


ORDERED_OPS = ("=", "<>", ">", ">=", "<", "<=", "IN", "BETWEEN")
NULL_OPS = ("IS NULL", "IS NOT NULL", "IS PRESENT", "IS ABSENT")
# a LIKE pattern is built only from a value holding none of these
LIKE_WILDCARDS = "%?_\\"


def ordered_ops(ftype) -> tuple:
    """Every comparison operator, on every ordered type: the reference models this
    engine's order inside each type."""
    return ORDERED_OPS
