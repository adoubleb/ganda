"""OrientDB's value semantics (live-probed 3.2.56, `scripts/probe.py`): the value
order and equality the oracle and the state check compare answers under, and the
arithmetic the generators and the guards walk the `Instance` with. How a scan answers
a predicate is `concrete_syntax/query/predicate_sampling/predicate_steering.py`; the
provider assembles both into the core's `Semantics` (`OrientSemantics`). No answer is
predicted from them. The rules:

- **Order.** LONG / INTEGER are Java integer order. DOUBLE / FLOAT are
  `Double.compare`: `-0.0 < 0.0`, so `d = 0` misses a stored `-0.0`, `d < 0` finds it,
  and GROUP BY keeps the two apart. DECIMAL is numeric order (no `-0.0`). STRING is
  `String.compareTo`, UTF-16 code-unit order: an astral code point (two surrogates,
  `D83D DE00`) sorts BELOW U+FFFF, where Python's code-point order puts it above --
  `orient_key` compares the UTF-16 encoding. No Unicode normalization: an NFD `e` +
  U+0301 is two units, distinct from U+00E9.
- **Aggregates.** `sum` over LONGs is exact long arithmetic (it wraps past 2^63, so a
  sum is only emitted where every partial sum fits); `sum` over INTEGERs is exact INT
  arithmetic (it wraps past 2^31, O19: only emitted where every partial sum fits an
  int); `sum` over DECIMALs is exact decimal arithmetic. `max`/`min` ignore null and
  return null over nothing; `sum` over nothing is 0; `.size()` counts elements, null
  included; `.asSet()` deduplicates by value.
- **Dates.** A DATETIME / DATE value is its `yyyy-MM-dd HH:mm:ss` string (UTC; a
  DATE at midnight), on which lexicographic order is chronological order. A constant
  is normalized the way the engine converts it: a date-only string is that day's
  midnight, an epoch-millisecond integer its second (millis dropped, as the write
  path drops them); a string the format only partially matches (an ISO `T`, no
  seconds) is what it SAYS -- the engine compares it as the date's midnight, O27,
  withheld.
"""
from __future__ import annotations

import datetime as _dt
import math
import re
import struct
from decimal import Decimal
from typing import Any

from ganda.core.constants import I32_MAX, I32_MIN
from ganda.core.data.model import DATE_TYPES, FieldType
from ganda.core.data.neighbours import _f32, shift_date
from ganda.orientdb.rules import answer, assumes, works_around
from ganda.orientdb.rules.catalog import SQL



def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def f32(v: float) -> float:
    """The float32 nearest to `v`, widened back to a double (what a FLOAT column
    stores, and what a Float compares as)."""
    try:
        return struct.unpack("f", struct.pack("f", v))[0]
    except OverflowError:
        return v


def decimal_of(v) -> Decimal:
    """What `BigDecimal.valueOf(double)` stores for a double: its shortest decimal."""
    if isinstance(v, float):
        return Decimal(repr(v))
    return Decimal(v)


def decimal_binary(v) -> Decimal:
    """What a scan compares a double constant as: `new BigDecimal(double)`, the exact
    binary expansion (O20)."""
    if isinstance(v, float):
        return Decimal(v)
    return Decimal(v)


_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CANON = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
_EPOCH = _dt.datetime(1970, 1, 1)


def canonical_date(v: Any) -> Any:
    """A date constant as the engine converts it for a DATETIME / DATE column: a
    canonical string stays; a date-only string becomes midnight; an epoch
    millisecond integer becomes its second; an ISO `T` separator or a `Z` /
    fractional-second suffix is parsed as what it says (the engine parses the `T`
    form and a missing-seconds form as midnight, O27: the guard withholds those)."""
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, int):
        return (_EPOCH + _dt.timedelta(milliseconds=v)).replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, float):
        return canonical_date(int(v))
    if isinstance(v, str):
        if _CANON.match(v):
            return v
        if _DATE_ONLY.match(v):
            return v + " 00:00:00"
        m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(?::(\d{2}))?(?:\.\d+)?Z?$", v)
        if m:
            return f"{m.group(1)} {m.group(2)}:{m.group(3) or '00'}"
    return v


# a comparison orders strings by UTF-16 code unit, keeps -0.0 below 0.0
assumes("compare.negative_zero_below_zero", SQL, True)


def orient_key(v: Any):
    """A total-order key reproducing OrientDB's comparison inside one declared type,
    and a stable cross-type rank for mixed lists (null < bool < number < string <
    list)."""
    if v is None:
        return (0,)
    if isinstance(v, bool):
        return (1, int(v))
    if isinstance(v, int):
        return (2, v, 0)
    if isinstance(v, float):
        if v == 0:
            return (2, 0, -1 if str(v).startswith("-") else 0)
        return (2, v, 0)
    if isinstance(v, Decimal):
        return (2, v, 0)
    if isinstance(v, str):
        return (3, v.encode("utf-16-be"))
    if isinstance(v, list):
        return (4, tuple(orient_key(x) for x in v))
    if isinstance(v, dict):
        return (5, tuple(sorted((k, orient_key(x)) for k, x in v.items())))
    return (6, repr(v))


def eq(a: Any, b: Any) -> bool:
    """OrientDB equality inside one declared type: `Double.compare` for doubles (so
    `-0.0` and `0.0` differ), exact otherwise. A bool never equals a number."""
    if a is None or b is None:
        return False
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if _is_number(a) and _is_number(b):
        return orient_key(float(a) if isinstance(a, float) or isinstance(b, float) else a) == orient_key(float(b) if isinstance(a, float) or isinstance(b, float) else b)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(eq(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def lt(a: Any, b: Any) -> bool:
    """Strict order inside one declared type; a LONG against a DOUBLE goes through
    double (probed: `2^53 + 1 = 9007199254740992.0`, `-0.0 < 0`)."""
    if _is_number(a) and _is_number(b) and (isinstance(a, float) or isinstance(b, float)):
        return orient_key(float(a)) < orient_key(float(b))
    return orient_key(a) < orient_key(b)




assumes("exact.sum.decimal", SQL, True)


def sum_is_exact(values: list, ftype: "str | None" = "int") -> bool:
    """Every partial sum in every order stays in the engine's arithmetic: ints with an
    absolute total below 2^62 (half the long range, so no interleaving of signs can
    wrap) for a LONG column, below 2^30 for an INTEGER column (`sum` accumulates in
    32-bit arithmetic, O19); a DECIMAL sum is exact."""
    if ftype == "decimal":
        return all(isinstance(v, float) for v in values)
    if any(isinstance(v, bool) or not isinstance(v, int) for v in values):
        return False
    bound = (answer("exact.sum.int32.below", SQL) if ftype == "int32" else answer("aggregate.sum.integer.exact_below", SQL)) // 2
    return sum(abs(v) for v in values) < bound


def apply_arith(v, sym: str, c, ftype: "str | None"):
    """`v <sym> c` as the engine computes it where the arithmetic is exact (the
    caller guarded that): integer arithmetic on the integer types, double / float
    arithmetic (exact by construction) on the floating ones, exact decimal
    arithmetic on DECIMAL, a millisecond shift on the date types."""
    if ftype in ("datetime", "date"):
        return shift_date(v, (c if sym == "+" else -c) // 1000)
    if ftype == "decimal":
        a = Decimal(repr(v)) if isinstance(v, float) else Decimal(v)
        r = {"+": a + c, "-": a - c, "*": a * c}[sym]
        return float(r)
    return {"+": v + c, "-": v - c, "*": v * c}[sym]






def lossy_constant(ftype: FieldType, v) -> bool:
    """Whether converting `v` to the column's type changes it -- the constants an index
    lookup (which converts the key) and a scan (which compares as the wider box)
    can disagree on: a double with a fractional part or at/after 2^53 on a LONG
    column; on an INTEGER column that, or a Long outside int32 (wrapped); a double
    that is not float32-exact on a FLOAT column; on a DECIMAL column a double whose
    binary expansion is not its decimal string (every non-dyadic one)."""
    if isinstance(v, bool) or v is None:
        return False
    if ftype is FieldType.INT:
        return isinstance(v, float) and math.isfinite(v) and (v != int(v) or abs(v) >= 2**53)
    if ftype is FieldType.INT32:
        if isinstance(v, float):
            return not math.isfinite(v) or v != int(v) or not (I32_MIN <= v <= I32_MAX)
        return isinstance(v, int) and not (I32_MIN <= v <= I32_MAX)
    if ftype is FieldType.FLOAT32:
        return isinstance(v, float) and _f32(v) != v
    if ftype is FieldType.DECIMAL:
        if not isinstance(v, float) or not math.isfinite(v):
            return False
        return Decimal(v) != Decimal(repr(v))
    if ftype in DATE_TYPES:
        # a string the format only partially matches: the engine compares it as the
        # date's midnight (O27); a date-only string or an epoch integer converts
        # faithfully
        return isinstance(v, str) and ("T" in v or (len(v) == 16 and v[10] == " "))
    return False


# an out-of-range write into an INTEGER column is stored wrapped (O24): a write whose
# result leaves int32 is never drawn
works_around("O24")


def arith_exact(v, sym: str, c, ftype) -> bool:
    """Whether `v <sym> c` is computed exactly in the column's Java box (`ftype` a
    `FieldType`): an int (a write stores an out-of-range result wrapped, O24), a long,
    a float32-exact float, a double scaled by a power of two, a decimal with at most
    three places, a date inside the format's range."""
    from ganda.core.constants import I64_MAX, I64_MIN

    if v is None:
        return True
    try:
        r = apply_arith(v, sym, c, ftype.value)
    except (ValueError, OverflowError):
        return False
    if ftype is FieldType.INT32:
        return I32_MIN <= r <= I32_MAX
    if ftype is FieldType.INT:
        return I64_MIN <= r <= I64_MAX
    if ftype is FieldType.FLOAT32:
        return math.isfinite(r) and f32(r) == r and abs(r) < 2**24
    if ftype is FieldType.FLOAT:
        return math.isfinite(r) and r != 0 or v == 0
    if ftype is FieldType.DECIMAL:
        return math.isfinite(r) and not lossy_constant(FieldType.DECIMAL, r) and abs(r) < 1e15
    if ftype in DATE_TYPES:
        # `_shift_date` answers its input unchanged past year 9999: that is no shift
        # (`f + 1000 <> '9999-12-31 23:59:59'` is not `f <> ...`, smoke sel-smoke4
        # seed 1101)
        return isinstance(r, str) and r != v and "1900-01-01 00:00:00" <= r <= "9999-12-31 23:59:59"
    return False
