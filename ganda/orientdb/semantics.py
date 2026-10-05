"""The independent oracle leg: expected answers computed from the `Instance` itself,
under OrientDB's value semantics (live-probed 3.2.56, `scripts/probe_osql.py`).

Pairwise comparison cannot see a bug every representation shares; this leg can (the
`all_sides_drop_edge` mutant proves it works). What Python reproduces exactly here:

- **Order.** LONG / INTEGER are Java integer order. DOUBLE / FLOAT are
  `Double.compare`: `-0.0 < 0.0`, so `d = 0` misses a stored `-0.0`, `d < 0` finds it,
  and GROUP BY keeps the two apart. DECIMAL is numeric order (no `-0.0`). STRING is
  `String.compareTo`, UTF-16 code-unit order: an astral code point (two surrogates,
  `D83D DE00`) sorts BELOW U+FFFF, where Python's code-point order puts it above --
  `orient_key` compares the UTF-16 encoding. No Unicode normalization: an NFD `e` +
  U+0301 is two units, distinct from U+00E9.
- **Null.** An absent field and an explicit null answer alike: `IS NULL` holds, `<>`
  holds, and every other comparison (`=`, `<`, `>`, `IN`, `BETWEEN`, `LIKE`) fails.
  `IS DEFINED` tells them apart (absent: false).
- **Aggregates.** `sum` over LONGs is exact long arithmetic (it wraps past 2^63, so a
  sum is only emitted where every partial sum fits); `sum` over INTEGERs is exact INT
  arithmetic (it wraps past 2^31, O19: only emitted where every partial sum fits an
  int); `sum` over DECIMALs is exact decimal arithmetic. `max`/`min` ignore null and
  return null over nothing; `sum` over nothing is 0; `.size()` counts elements, null
  included; `.asSet()` deduplicates by value.
- **Cross-type comparison on a scan** (`castComparableNumber`: the wider Java box,
  Integer < Long < Float < Double): a LONG or INTEGER against a double literal goes
  through double (`2^53 + 1 = 9007199254740992.0` is true); a FLOAT against an
  integral pivot goes through FLOAT (`16777216.0f = 16777217`), against a double
  through double (a FLOAT value is its exact widening); a DECIMAL against an integer
  exactly and against a double through `new BigDecimal(double)`, the double's binary
  expansion (`0.1 = 0.1` is FALSE on a scan; O20 -- an index and a BETWEEN bound use
  the decimal string instead, which the reference models for BETWEEN and the guard
  withholds for an index).
- **Collation `ci`.** `=`, `<>`, `<`, `<=`, `>`, `>=` compare the lower-cased strings;
  `IN` and `BETWEEN` do NOT (O21, a scan compares code units); `LIKE` is
  case-insensitive on EVERY string column, collated or not (probed), with `%` any
  run and `?` one character (`_` is a literal).
- **Dates.** A DATETIME / DATE value is its `yyyy-MM-dd HH:mm:ss` string (UTC; a
  DATE at midnight), on which lexicographic order is chronological order. A pivot
  is normalized the way the engine converts it: a date-only string is that day's
  midnight, an epoch-millisecond integer its second (millis dropped, as the write
  path drops them); a string the format only partially matches (an ISO `T`, no
  seconds) is what it SAYS -- the engine compares it as the date's midnight, O27,
  withheld.
- **Maps.** A dotted map key (`m.k2 op :p`) reads the entry, a missing key or a
  missing map being null under the usual null rules; `CONTAINSKEY` tests the key,
  `m.values() CONTAINS` an element; `CONTAINSVALUE` with an Integer-boxed pivot on
  an `EMBEDDEDMAP LONG` matches nothing (an O7 face, withheld).
"""
from __future__ import annotations

import datetime as _dt
import re
import struct
from decimal import Decimal
from typing import Any

from ganda.orientdb.model import FieldType

I64_MAX = 2**63 - 1
I64_MIN = -(2**63)
I32_MAX = 2**31 - 1
I32_MIN = -(2**31)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def f32(v: float) -> float:
    """The float32 nearest to `v`, widened back to a double (what a FLOAT column
    stores, and what a Float compares as)."""
    try:
        return struct.unpack("f", struct.pack("f", v))[0]
    except OverflowError:
        return v


def f32_exact(v) -> bool:
    return isinstance(v, bool) or not isinstance(v, float) or f32(v) == v


def decimal_of(v) -> Decimal:
    """What `BigDecimal.valueOf(double)` stores for a double: its shortest decimal."""
    if isinstance(v, float):
        return Decimal(repr(v))
    return Decimal(v)


def decimal_binary(v) -> Decimal:
    """What a scan compares a double pivot as: `new BigDecimal(double)`, the exact
    binary expansion (O20)."""
    if isinstance(v, float):
        return Decimal(v)
    return Decimal(v)


_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CANON = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
_EPOCH = _dt.datetime(1970, 1, 1)


def canonical_date(v: Any) -> Any:
    """A date pivot as the engine converts it for a DATETIME / DATE column: a
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


# what Java's regex `.` does not match: the engine compiles a LIKE pattern into a
# regex without DOTALL (O42)
_LINE_TERMINATORS = "\n\r\u0085  "
_WILD = f"[^{_LINE_TERMINATORS}]"


def like_hit(value: str, pattern: str) -> bool:
    """`LIKE`: case-insensitive on every string (probed 3.2.56: `'a' LIKE 'A%'`
    holds on a plain STRING); `%` any run of characters and `?` one character --
    but NEITHER across a line terminator (O42, `issue-drafts/upstream/O42-...`: the
    pattern is a Java regex without DOTALL, so `'a\\nb' LIKE 'a%'` is false). The
    generator never builds a pattern from a value holding `%`, `?` or `\\`."""
    value, pattern = value.lower(), pattern.lower()
    rx = "".join(f"{_WILD}*" if c == "%" else _WILD if c == "?" else re.escape(c) for c in pattern)
    return re.fullmatch(rx, value) is not None


_CI_OPS = ("=", "<>", "<", "<=", ">", ">=")


def _convert(v: Any, pivot: Any, ftype: "str | None", ci: bool, op: str):
    """(stored, pivot) as the SCAN compares them for a column of `ftype`."""
    if ci and isinstance(v, str) and isinstance(pivot, str) and op in _CI_OPS:
        return v.lower(), pivot.lower()
    if ftype in ("datetime", "date"):
        return v, canonical_date(pivot)
    if ftype == "float32" and _is_number(pivot) and not isinstance(pivot, float):
        # an integral pivot against a Float compares through FLOAT
        return v, f32(float(pivot))
    if ftype == "decimal" and _is_number(v):
        if op == "BETWEEN":
            return decimal_of(v), decimal_of(pivot)
        return decimal_of(v), decimal_binary(pivot)
    return v, pivot


def predicate_hit(present: bool, v: Any, op: str, pivot: Any, ftype: "str | None" = None, ci: bool = False, key: "str | None" = None) -> bool:
    """What a class SCAN answers (probed 3.2.56) for a column of `ftype`
    (`FieldType.value`; None for an untyped embedded value) with collation `ci`.
    The null rules: a null or absent `v` satisfies `IS NULL` and `<>` and nothing
    else; a null PIVOT (a bound value that is null, as a value join binds) satisfies
    `<>` for every row, `<=` / `>=` for the null rows only (`null <= null` holds
    where `null = null` does not), and nothing else; a null element of an `IN` list
    matches nothing; a null `BETWEEN` bound matches nothing. With `key`, `v` is a
    map and the comparison reads its entry (`m.k op :p`), a missing map or key being
    null; the map's own operators are `CONTAINSKEY`, `VALUES CONTAINS` and
    `CONTAINSVALUE`."""
    if key is not None:
        if op in ("CONTAINSKEY", "VALUES CONTAINS", "CONTAINSVALUE"):
            raise ValueError(op)
        present = isinstance(v, dict) and key in v
        v = v.get(key) if isinstance(v, dict) else None
        ftype = "int"
    if op == "CONTAINSKEY":
        return isinstance(v, dict) and pivot in v
    if op == "VALUES CONTAINS":
        return isinstance(v, dict) and any(x is not None and eq(x, pivot) for x in v.values())
    if op == "CONTAINSVALUE":
        # the declared LONG values are Long boxes; an Integer-boxed pivot never
        # equals one (O7's map face); a Long or a double compares through its box
        if isinstance(pivot, int) and not isinstance(pivot, bool) and -(2**31) <= pivot < 2**31:
            return False
        return isinstance(v, dict) and any(x is not None and eq(x, pivot) for x in v.values())
    if op == "IS NULL":
        return not present or v is None
    if op == "IS NOT NULL":
        return present and v is not None
    if op == "IS DEFINED":
        return present
    if op == "IS NOT DEFINED":
        return not present
    null_v = not present or v is None
    if op == "<>":
        if null_v or pivot is None:
            return True
        a, b = _convert(v, pivot, ftype, ci, op)
        return not eq(a, b)
    if op in ("<=", ">=") and pivot is None:
        return null_v
    if null_v or pivot is None:
        return False
    if op == "LOWER=":
        return isinstance(v, str) and v.lower() == pivot
    if op == "=":
        a, b = _convert(v, pivot, ftype, ci, op)
        return eq(a, b)
    if op == "IN":
        return any(p is not None and eq(*_convert(v, p, ftype, ci, op)) for p in pivot)
    if op == "NOT IN":
        return not any(p is not None and eq(*_convert(v, p, ftype, ci, op)) for p in pivot)
    if op == "BETWEEN":
        lo, hi = pivot
        if lo is None or hi is None:
            return False
        a, lo = _convert(v, lo, ftype, ci, op)
        _, hi = _convert(v, hi, ftype, ci, op)
        return not lt(a, lo) and not lt(hi, a)
    if op == "LIKE":
        return isinstance(v, str) and like_hit(v, pivot)
    if op == "CONTAINS":
        return isinstance(v, list) and any(eq(x, pivot) for x in v)
    if op == "CONTAINSANY":
        return isinstance(v, list) and any(eq(x, pv) for pv in pivot for x in v)
    if op == "CONTAINSALL":
        # vacuous over an empty pivot list (probed: `f CONTAINSALL []` holds for
        # every non-null list)
        return isinstance(v, list) and all(any(eq(x, pv) for x in v) for pv in pivot)
    a, b = _convert(v, pivot, ftype, ci, op)
    if op == ">":
        return lt(b, a)
    if op == ">=":
        return not lt(a, b)
    if op == "<":
        return lt(a, b)
    if op == "<=":
        return not lt(b, a)
    raise ValueError(op)


def mean(values: list):
    """`avg()` over present values: in the operands' arithmetic (O28) -- over
    integers the long quotient (the callers withhold an inexact one), which a float
    division loses above 2^53 (`avg` over one `2^53 + 1` is `2^53 + 1`, local
    smoke tri400c seed 5285)."""
    if values and all(isinstance(v, int) and not isinstance(v, bool) for v in values) and sum(values) % len(values) == 0:
        return sum(values) // len(values)
    return sum(values) / len(values)


def sum_is_exact(values: list, ftype: "str | None" = "int") -> bool:
    """Every partial sum in every order stays in the engine's arithmetic: ints with an
    absolute total below 2^62 (half the long range, so no interleaving of signs can
    wrap) for a LONG column, below 2^30 for an INTEGER column (`sum` accumulates in
    32-bit arithmetic, O19); a DECIMAL sum is exact."""
    if ftype == "decimal":
        return all(isinstance(v, float) for v in values)
    if any(isinstance(v, bool) or not isinstance(v, int) for v in values):
        return False
    bound = 2**30 if ftype == "int32" else 2**62
    return sum(abs(v) for v in values) < bound


def agg(fn: str, values: list, ftype: "str | None" = None):
    """`values` are the elements the engine aggregates (null included, as a list
    projection would carry them)."""
    present = [v for v in values if v is not None]
    if fn == "count":
        return len(values)
    if fn == "distinct_count":
        return len({orient_key(v) for v in values})
    if fn == "sum":
        if ftype == "decimal":
            # BigDecimal arithmetic, read back as the double of its decimal string
            return float(sum((decimal_of(v) for v in present), Decimal(0))) if present else 0
        return sum(present) if present else 0
    if not present:
        return None
    if fn == "max":
        return max(present, key=orient_key)
    if fn == "min":
        return min(present, key=orient_key)
    raise ValueError(fn)


def sort_values(values: list) -> list:
    return sorted(values, key=orient_key)
