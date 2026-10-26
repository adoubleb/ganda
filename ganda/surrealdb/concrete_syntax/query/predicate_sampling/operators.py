"""SurrealDB's predicate operators: the ones a leaf may draw, and the characters a
`LIKE` pattern never holds (the pattern becomes `string::starts_with` / `ends_with` /
`contains` / `=`, so only the wildcard itself is reserved)."""
from __future__ import annotations

ORDERED_OPS = ("=", "<>", ">", ">=", "<", "<=", "IN", "BETWEEN")
NULL_OPS = ("IS NULL", "IS NOT NULL", "IS PRESENT", "IS ABSENT")
LIKE_WILDCARDS = "%_"


def ordered_ops(ftype) -> tuple:
    """Every comparison operator on every ordered type: the engine orders each kind
    totally (strings by code point, numbers by value, datetimes chronologically)."""
    return ORDERED_OPS
