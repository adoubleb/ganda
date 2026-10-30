"""ArangoDB's predicate operators: the ones a leaf may draw per column type, and the
characters a `LIKE` pattern never holds.

A STRING (or BINARY) column takes the equality family only: AQL orders strings by ICU
collation (`order.string` in `rules/catalog.py`), which the generators' walk of the
Instance does not model, so an ordered comparison on one would steer nothing. The
model's date types are strings of one fixed shape, ordered alike by ICU and by code
point, so they keep every operator.
"""
from __future__ import annotations

from ganda.core.data.model import FieldType
from ganda.core.rules.vocabulary import StringOrder
from ganda.arangodb.rules import assumes
from ganda.arangodb.rules.catalog import AQL
from ganda.arangodb.concrete_syntax.query.predicate_sampling.predicate_steering import _LINE_TERMINATORS

ORDERED_OPS = ("=", "<>", ">", ">=", "<", "<=", "IN", "BETWEEN")
EQUALITY_OPS = ("=", "<>", "IN")
NULL_OPS = ("IS NULL", "IS NOT NULL", "IS PRESENT", "IS ABSENT")
# a LIKE pattern is built only from a value holding none of these: the wildcards, the
# escape, and the line terminators an ICU regex's `.` and `$` treat specially
LIKE_WILDCARDS = "%_\\\n\r" + _LINE_TERMINATORS
# a string column's order (ICU collation) is no order the Instance walk reproduces
assumes("order.string", AQL, StringOrder.COLLATION)


def ordered_ops(ftype: FieldType) -> tuple:
    """The comparison operators a leaf on a column of `ftype` may draw."""
    if ftype in (FieldType.STRING, FieldType.BINARY):
        return EQUALITY_OPS
    return ORDERED_OPS
