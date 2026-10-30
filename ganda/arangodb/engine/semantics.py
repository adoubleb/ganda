"""AQL's value semantics: a total type order (null < bool < number < string < array <
object), exact number comparison, code-point string equality, ICU string order."""
from __future__ import annotations

from typing import Any

from ganda.core.rules.vocabulary import StringOrder
from ganda.arangodb.rules import answer, assumes
from ganda.arangodb.rules.catalog import AQL

# the mirror below has no branch for another answer to this
assumes("equality.string", AQL, StringOrder.CODE_POINT)
# sums run in double: exact over integers whose absolute total stays under this
F64_INT_EXACT = answer("aggregate.sum.integer.exact_below", AQL)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def rank(v: Any) -> int:
    if v is None:
        return 0
    if isinstance(v, bool):
        return 1
    if _is_number(v):
        return 2
    if isinstance(v, str):
        return 3
    if isinstance(v, list):
        return 4
    return 5


def _num(v):
    """A number as a comparison key: a float zero loses its sign, an integer stays
    exact (two 64-bit integers compare exactly; an integer against a double goes
    through double, which `eq` / `lt` handle on the pair)."""
    if isinstance(v, float) and v == 0:
        return 0.0
    return v


def aql_key(v: Any):
    """A total-order key reproducing AQL's comparison inside one type and its rank
    across types; strings by code point (the one place AQL differs: ICU collation,
    which no consumer of the key relies on)."""
    if v is None:
        return (0,)
    if isinstance(v, bool):
        return (1, int(v))
    if _is_number(v):
        return (2, _num(v))
    if isinstance(v, str):
        return (3, v)
    if isinstance(v, list):
        return (4, tuple(aql_key(x) for x in v))
    if isinstance(v, dict):
        return (5, tuple(sorted((k, aql_key(x)) for k, x in v.items())))
    return (6, repr(v))


def eq(a: Any, b: Any) -> bool:
    """AQL `==`: total, so two nulls are equal; a bool never equals a number; numbers
    compare exactly whatever their JSON form; lists element by element."""
    if rank(a) != rank(b):
        return False
    if _is_number(a):
        # exact: a 64-bit integer against a double is NOT rounded through double
        # (probed 2026-10-24: `2^53 + 1 == 9007199254740992.0` is false)
        return a == b
    if isinstance(a, list):
        return len(a) == len(b) and all(eq(x, y) for x, y in zip(a, b))
    if isinstance(a, dict):
        return set(a) == set(b) and all(eq(a[k], b[k]) for k in a)
    return a == b


def lt(a: Any, b: Any) -> bool:
    """AQL `<`: the type order first, then the value (strings by code point, an
    approximation no predicate relies on)."""
    ra, rb = rank(a), rank(b)
    if ra != rb:
        return ra < rb
    if ra == 0:
        return False
    if _is_number(a):
        return a < b  # exact, as `eq`
    if isinstance(a, list):
        for x, y in zip(a, b):
            if not eq(x, y):
                return lt(x, y)
        return len(a) < len(b)
    if isinstance(a, dict):
        return aql_key(a) < aql_key(b)
    return a < b


def sum_is_exact(values: list, ftype: "str | None" = "int") -> bool:
    """Every partial sum in every order stays exact in double arithmetic: integers
    whose absolute total is under 2^53. A double sum is never shown exact."""
    if any(isinstance(v, bool) or not isinstance(v, int) for v in values):
        return False
    return sum(abs(v) for v in values) < F64_INT_EXACT


def apply_arith(v, sym: str, c, ftype: "str | None"):
    """`v <sym> c` where the arithmetic is exact (the caller guarded that): plain
    number arithmetic."""
    return {"+": v + c, "-": v - c, "*": v * c}[sym]


# integer arithmetic is exact under this magnitude (AQL arithmetic runs in double past it)
ARITHMETIC_EXACT_BELOW = answer("exact.arithmetic.integer.below", AQL)


def arith_exact(v, sym: str, c, ftype) -> bool:
    """Whether `v <sym> c` is computed exactly (`ftype` a `FieldType`): integer
    arithmetic whose result stays under 2^53 in magnitude (AQL arithmetic runs in double
    past that), a double scaled by a power of two or negated. A string (a date) takes no
    arithmetic."""
    import math

    from ganda.core.data.model import FLOAT_TYPES, INT_TYPES

    if v is None:
        return True
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    try:
        r = apply_arith(v, sym, c, ftype.value)
    except (ValueError, OverflowError):
        return False
    if ftype in INT_TYPES:
        return isinstance(r, int) and abs(r) < ARITHMETIC_EXACT_BELOW
    if ftype in FLOAT_TYPES:
        return math.isfinite(r) and sym == "*" and (r != 0 or v == 0)
    return False


def canonical(value):
    if isinstance(value, float) and value.is_integer() and abs(value) < 2**63:
        return int(value)
    if isinstance(value, list):
        return [canonical(v) for v in value]
    if isinstance(value, dict):
        return {k: canonical(v) for k, v in value.items()}
    return value
