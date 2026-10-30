"""ArangoDB's predicate operators per column type. A string column takes the equality
family only: AQL orders strings by ICU collation, which steering does not model."""
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
