"""OrientDB's value semantics (live-probed 3.2.56, `scripts/probe_osql.py`): the value
order and equality the oracle and the state check compare answers under, and the
predicate logic and arithmetic the generators and the guards walk the `Instance` with.
No answer is predicted from them. The rules:

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
  the decimal string instead, which `predicate_hit` follows for BETWEEN and the guard
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
import math
import re
import struct
from decimal import Decimal
from typing import Any

from ganda.core.query.ir.predicates import Compound
from ganda.core.data.neighbours import shift_date
from ganda.core.engine.provider import Semantics
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


# what Java's regex `.` does not match: the engine compiles a LIKE pattern into a
# regex without DOTALL
_LINE_TERMINATORS = "\n\r\u0085\u2028\u2029"
_WILD = f"[^{_LINE_TERMINATORS}]"



def like_hit(value: str, pattern: str) -> bool:
    """`LIKE`: case-insensitive on every string (probed 3.2.56: `'a' LIKE 'A%'`
    holds on a plain STRING); `%` any run of characters and `?` one character --
    but NEITHER across a line terminator (the pattern is a Java regex without DOTALL, so `'a\\nb' LIKE 'a%'` is false). The
    generator never builds a pattern from a value holding `%`, `?` or `\\`."""
    value, pattern = value.lower(), pattern.lower()
    rx = "".join(f"{_WILD}*" if c == "%" else _WILD if c == "?" else re.escape(c) for c in pattern)
    return re.fullmatch(rx, value) is not None


# the comparisons a ci column makes case-insensitively on a scan (generation only)
_CI_OPS = tuple(answer("filter.case_insensitive.operators", SQL).split())


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


# `IN` over a long list (a semi-join's answered values: thousands of them over a large
# class) is answered through an index over the list, built once per list. It is exact:
# inside one declared type `eq` IS set membership -- an int or a string or a bool by
# itself, a double by `orient_key` (which keeps `-0.0` apart from `0.0`), a decimal by
# the converted `Decimal` the scan compares -- and a list that mixes kinds (a cross-type
# pivot) keeps the element-by-element path.
_IN_INDEX: dict = {}
_IN_INDEX_MAX = 64
_IN_INDEX_MIN = 8


def _in_index(pivot: list, ftype: "str | None", ci: bool):
    cache_key = (id(pivot), ftype, ci)
    hit = _IN_INDEX.get(cache_key)
    if hit is not None and hit[0] is pivot:
        return hit[1]
    present = [p for p in pivot if p is not None]
    index = None
    try:
        if ftype == "decimal":
            if all(_is_number(p) for p in present):
                index = ("decimal", {decimal_binary(p) for p in present})
        else:
            # the pivot's conversion for a non-decimal column reads nothing of the
            # stored value (`IN` is not collated: O21)
            converted = [_convert(None, p, ftype, ci, "IN")[1] for p in present]
            if all(isinstance(c, bool) for c in converted):
                index = ("bool", set(converted))
            elif all(isinstance(c, int) and not isinstance(c, bool) for c in converted):
                index = ("int", set(converted))
            elif all(isinstance(c, float) for c in converted):
                index = ("float", {orient_key(c) for c in converted})
            elif all(isinstance(c, str) for c in converted):
                index = ("str", set(converted))
    except (ValueError, OverflowError, TypeError, ArithmeticError):
        # an element the column's conversion rejects: the element-by-element path
        # meets it exactly as it did
        index = None

    if len(_IN_INDEX) >= _IN_INDEX_MAX:
        _IN_INDEX.clear()
    _IN_INDEX[cache_key] = (pivot, index)
    return index


def _in_hit(v: Any, pivot: list, ftype: "str | None", ci: bool) -> bool:
    """`v IN pivot` on a scan: a null element matches nothing."""
    if len(pivot) >= _IN_INDEX_MIN:
        index = _in_index(pivot, ftype, ci)
        if index is not None:
            kind, members = index
            if kind == "int" and isinstance(v, int) and not isinstance(v, bool):
                return v in members
            if kind == "float" and isinstance(v, float):
                return orient_key(v) in members
            if kind == "str" and isinstance(v, str):
                return v in members
            if kind == "bool" and isinstance(v, bool):
                return v in members
            if kind == "decimal" and _is_number(v):
                return decimal_of(v) in members
    return any(p is not None and eq(*_convert(v, p, ftype, ci, "IN")) for p in pivot)


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
        if op in ("HAS KEY", "VALUES HAS", "HAS VALUE"):
            raise ValueError(op)
        present = isinstance(v, dict) and key in v
        v = v.get(key) if isinstance(v, dict) else None
        ftype = "int"
    if op == "HAS KEY":
        return isinstance(v, dict) and pivot in v
    if op == "VALUES HAS":
        return isinstance(v, dict) and any(x is not None and eq(x, pivot) for x in v.values())
    if op == "HAS VALUE":
        # the declared LONG values are Long boxes; an Integer-boxed pivot never
        # equals one (O7's map face); a Long or a double compares through its box
        if isinstance(pivot, int) and not isinstance(pivot, bool) and -(2**31) <= pivot < 2**31:
            return False
        return isinstance(v, dict) and any(x is not None and eq(x, pivot) for x in v.values())
    if op == "IS NULL":
        return not present or v is None
    if op == "IS NOT NULL":
        return present and v is not None
    if op == "IS PRESENT":
        return present
    if op == "IS ABSENT":
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
    if op == "=":
        a, b = _convert(v, pivot, ftype, ci, op)
        return eq(a, b)
    if op == "IN":
        return _in_hit(v, pivot, ftype, ci)
    if op == "NOT IN":
        return not _in_hit(v, pivot, ftype, ci)

    if op == "BETWEEN":
        lo, hi = pivot
        if lo is None or hi is None:
            return False
        a, lo = _convert(v, lo, ftype, ci, op)
        _, hi = _convert(v, hi, ftype, ci, op)
        return not lt(a, lo) and not lt(hi, a)
    if op == "LIKE":
        return isinstance(v, str) and like_hit(v, pivot)
    if op == "HAS":
        return isinstance(v, list) and any(eq(x, pivot) for x in v)
    if op == "HAS ANY":
        return isinstance(v, list) and any(eq(x, pv) for pv in pivot for x in v)
    if op == "HAS ALL":
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




def evaluate(values: dict, p) -> bool:
    """Two-valued boolean logic over `predicate_hit` for the leaves (probed 3.2.56:
    `NOT (f > 5)` holds for a null `f`)."""
    if isinstance(p, Compound):
        if p.op == "NOT":
            return not evaluate(values, p.children[0])
        if p.op == "AND":
            return all(evaluate(values, c) for c in p.children)
        return any(evaluate(values, c) for c in p.children)
    present, v = p.field in values, values.get(p.field)
    if p.sub is not None:
        # the listed values are the engine's own stored values of the same column
        # type, compared as stored (a DECIMAL against a BigDecimal, not through a
        # double's expansion; a ci column code unit by code unit, as a scan's `IN`
        # does): no conversion (the semi-join leaf's reference)
        return predicate_hit(present, v, "IN", p.pivot)
    if p.arith is not None and v is not None and p.key is None:
        v = apply_arith(v, p.arith.sym, p.arith.value, p.ftype)
    return predicate_hit(present, v, p.op, p.pivot, p.ftype, p.case_insensitive, p.key)


# an out-of-range write into an INTEGER column is stored wrapped (O24): a write whose
# result leaves int32 is never drawn
works_around("O24")


def arith_exact(v, sym: str, c, ftype) -> bool:
    """Whether `v <sym> c` is computed exactly in the column's Java box (`ftype` a
    `FieldType`): an int (a write stores an out-of-range result wrapped, O24), a long,
    a float32-exact float, a double scaled by a power of two, a decimal with at most
    three places, a date inside the format's range."""
    from ganda.core.constants import I32_MAX, I32_MIN, I64_MAX, I64_MIN
    from ganda.core.data.model import DATE_TYPES, FieldType
    from ganda.orientdb.oracle.pivots import lossy_pivot

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
        return math.isfinite(r) and not lossy_pivot(FieldType.DECIMAL, r) and abs(r) < 1e15
    if ftype in DATE_TYPES:
        # `_shift_date` answers its input unchanged past year 9999: that is no shift
        # (`f + 1000 <> '9999-12-31 23:59:59'` is not `f <> ...`, smoke sel-smoke4
        # seed 1101)
        return isinstance(r, str) and r != v and "1900-01-01 00:00:00" <= r <= "9999-12-31 23:59:59"
    return False


class OrientSemantics(Semantics):
    """OrientDB's value semantics as the core's `Semantics`: this module's functions."""

    key = staticmethod(orient_key)
    eq = staticmethod(eq)
    lt = staticmethod(lt)
    predicate_hit = staticmethod(predicate_hit)
    evaluate = staticmethod(evaluate)
    apply_arith = staticmethod(apply_arith)
    arith_exact = staticmethod(arith_exact)
    sum_is_exact = staticmethod(sum_is_exact)

SEMANTICS = OrientSemantics()
