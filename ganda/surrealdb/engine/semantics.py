"""SurrealDB's value semantics (live-probed 3.3.4, `rules/probe.py`): the value order and
equality the oracle and the state check compare answers under, and the arithmetic the
generators and the guards walk the `Instance` with. How a scan answers a predicate is
`concrete_syntax/query/predicate_sampling/predicate_steering.py`; the provider assembles
both into the core's `Semantics` (`SurrealSemantics`). No answer is predicted from them.
The rules:

- **Order.** Values of different kinds are totally ordered: NONE < NULL < bool < number
  < string < datetime < array < object. Numbers compare by value across int, float and
  decimal (`1 = 1.0`, `0.1dec = 0.1`; `-0.0 = 0.0`). Strings compare by code point (a
  Rust `String`; no collation, no normalization).
- **Arithmetic.** `+` on two ints is refused past the 64-bit range (the statement
  errors), where `math::sum` wraps silently (F26); decimals are exact to 28 digits;
  floats are doubles. A null or absent operand is refused too (`null + 1` errors), so
  no arithmetic is asked of a value that may be missing.
- **Aggregates.** `math::sum([])` is 0, `math::max([])` / `math::min([])` / `math::mean([])`
  are NONE; every one refuses a NULL or NONE element, so a sum is asked only over the
  values present.
- **Wire forms.** A decimal arrives as its text (`"1.5"`, `"5"` for `5.0dec`), a datetime
  as ISO text (`"1970-01-01T00:00:00Z"`), bytes as a list of byte values; every data
  model answers the same form, so nothing is canonicalized.
"""
from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

from ganda.core.constants import I64_MAX, I64_MIN
from ganda.core.data.model import DATE_TYPES, FieldType
from ganda.surrealdb.rules import answer, assumes
from ganda.surrealdb.rules.catalog import SURQL


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


assumes("compare.int_float.by_value", SURQL, True)
assumes("compare.negative_zero.equals_zero", SURQL, True)
assumes("order.string", SURQL, "code-point")


def surreal_key(v: Any):
    """A total-order key reproducing SurrealDB's comparison inside one kind and its
    rank across kinds (NONE and NULL both below every value: the core's null)."""
    if v is None:
        return (0,)
    if isinstance(v, bool):
        return (1, int(v))
    if isinstance(v, (int, float)):
        return (2, v)  # Python compares an int with a float exactly; -0.0 == 0.0
    if isinstance(v, Decimal):
        return (2, v)
    if isinstance(v, str):
        return (3, v)
    if isinstance(v, list):
        return (4, tuple(surreal_key(x) for x in v))
    if isinstance(v, dict):
        return (5, tuple(sorted((k, surreal_key(x)) for k, x in v.items())))
    return (6, repr(v))


def eq(a: Any, b: Any) -> bool:
    """Equality as a comparison answers it: a null equals nothing (the core's `=`), a
    bool never equals a number, numbers compare by value, lists element by element."""
    if a is None or b is None:
        return False
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if _is_number(a) and _is_number(b):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(eq(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(eq(a[k], b[k]) for k in a)
    return type(a) is type(b) and a == b


def lt(a: Any, b: Any) -> bool:
    return surreal_key(a) < surreal_key(b)


# `math::sum` over ints wraps past the 64-bit range (F26): a sum is asked only where
# every partial sum stays inside it
_INT_SUM_BOUND = answer("aggregate.sum.integer.exact_below", SURQL) // 2
assumes("exact.sum.decimal", SURQL, True)
# a float sums exactly when every value is a dyadic rational of few bits
_DYADIC_BITS = 2**20
_FLOAT_SUM_BOUND = 2**50


def sum_is_exact(values: list, ftype: "str | None" = "int") -> bool:
    """Every partial sum in every order is exact: ints with an absolute total below
    half the 64-bit range; decimals (28 digits hold every pool value's sum); floats only
    when every value has at most 20 fractional bits and the total stays small."""
    if ftype == "decimal":
        return all(_is_number(v) for v in values)
    if ftype in ("float", "float32"):
        return all(_is_number(v) and math.isfinite(v) and (v * _DYADIC_BITS) == int(v * _DYADIC_BITS) for v in values) and sum(abs(v) for v in values) < _FLOAT_SUM_BOUND
    if any(isinstance(v, bool) or not isinstance(v, int) for v in values):
        return False
    return sum(abs(v) for v in values) < _INT_SUM_BOUND


def apply_arith(v, sym: str, c, ftype: "str | None"):
    """`v <sym> c` as the engine computes it where the arithmetic is exact (the caller
    guarded that): integer arithmetic, double arithmetic, exact decimal arithmetic."""
    if ftype == "decimal":
        a = Decimal(repr(v)) if isinstance(v, float) else Decimal(v)
        cc = Decimal(repr(c)) if isinstance(c, float) else Decimal(c)
        r = {"+": a + cc, "-": a - cc, "*": a * cc}[sym]
        return float(r)
    return {"+": v + c, "-": v - c, "*": v * c}[sym]


assumes("arithmetic.int.overflow_refused", SURQL, True)


def arith_exact(v, sym: str, c, ftype) -> bool:
    """Whether `v <sym> c` is computed exactly in a column of `ftype` (a `FieldType`):
    an int inside the 64-bit range (the engine refuses an overflow outright), a finite
    double that stays a dyadic rational, a decimal of at most 28 digits; never a date
    (its arithmetic takes durations) and never a missing value."""
    if v is None:
        return False
    if ftype in DATE_TYPES or isinstance(v, (str, bool)):
        return False
    try:
        r = apply_arith(v, sym, c, ftype.value)
    except (ValueError, OverflowError, ArithmeticError):
        return False
    if ftype in (FieldType.INT, FieldType.INT32):
        return isinstance(r, int) and I64_MIN <= r <= I64_MAX
    if ftype in (FieldType.FLOAT, FieldType.FLOAT32):
        return isinstance(r, float) and math.isfinite(r) and (r * _DYADIC_BITS) == int(r * _DYADIC_BITS) and abs(r) < _FLOAT_SUM_BOUND
    if ftype is FieldType.DECIMAL:
        # the exact decimal result must survive the round trip through the model's float
        exact = {"+": Decimal(repr(v)) + Decimal(repr(c)), "-": Decimal(repr(v)) - Decimal(repr(c)), "*": Decimal(repr(v)) * Decimal(repr(c))}[sym]
        return math.isfinite(r) and abs(r) < 1e15 and Decimal(repr(float(r))) == exact
    return False


def canonical(value):
    """Every data model answers the same wire form (the module's docstring): identity."""
    return value
