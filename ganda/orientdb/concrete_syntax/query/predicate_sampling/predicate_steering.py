"""How an OrientDB class SCAN answers a predicate over a record (live-probed 3.2.56,
`rules/probe.py`): the leaves (`predicate_hit`) and the two-valued logic over
them (`evaluate`). The generators steer with it (a filter that keeps some records, a
write that matches some), `write.apply` keeps the `Instance` in step with it, and the
guards read which records a defect's shape would touch; no answer is predicted from
it. Values are ordered and compared under `ganda.orientdb.engine.semantics`. The rules:

- **Null.** An absent field and an explicit null answer alike: `IS NULL` holds, `<>`
  holds, and every other comparison (`=`, `<`, `>`, `IN`, `BETWEEN`, `LIKE`) fails.
  `IS DEFINED` tells them apart (absent: false).
- **Cross-type comparison on a scan** (`castComparableNumber`: the wider Java box,
  Integer < Long < Float < Double): a LONG or INTEGER against a double literal goes
  through double (`2^53 + 1 = 9007199254740992.0` is true); a FLOAT against an
  integral constant goes through FLOAT (`16777216.0f = 16777217`), against a double
  through double (a FLOAT value is its exact widening); a DECIMAL against an integer
  exactly and against a double through `new BigDecimal(double)`, the double's binary
  expansion (`0.1 = 0.1` is FALSE on a scan; O20 -- an index and a BETWEEN bound use
  the decimal string instead, which `predicate_hit` follows for BETWEEN and the guard
  withholds for an index).
- **Collation `ci`.** `=`, `<>`, `<`, `<=`, `>`, `>=` compare the lower-cased strings;
  `IN` and `BETWEEN` do NOT (O21, a scan compares code units); `LIKE` is
  case-insensitive on EVERY string column, collated or not (probed), with `%` any
  run and `?` one character (`_` is a literal).
- **Maps.** A dotted map key (`m.k2 op :p`) reads the entry, a missing key or a
  missing map being null under the usual null rules; `CONTAINSKEY` tests the key,
  `m.values() CONTAINS` an element; `CONTAINSVALUE` with an Integer-boxed constant on
  an `EMBEDDEDMAP LONG` matches nothing (an O7 face, withheld).
"""
from __future__ import annotations

import re
from typing import Any

from ganda.core.query.ir.predicates import Compound
from ganda.orientdb.engine.semantics import _is_number, apply_arith, canonical_date, decimal_binary, decimal_of, eq, f32, lt, orient_key
from ganda.orientdb.rules import answer
from ganda.orientdb.rules.catalog import SQL


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


def _convert(v: Any, constant: Any, ftype: "str | None", ci: bool, op: str):
    """(stored, constant) as the SCAN compares them for a column of `ftype`."""
    if ci and isinstance(v, str) and isinstance(constant, str) and op in _CI_OPS:
        return v.lower(), constant.lower()
    if ftype in ("datetime", "date"):
        return v, canonical_date(constant)
    if ftype == "float32" and _is_number(constant) and not isinstance(constant, float):
        # an integral constant against a Float compares through FLOAT
        return v, f32(float(constant))
    if ftype == "decimal" and _is_number(v):
        if op == "BETWEEN":
            return decimal_of(v), decimal_of(constant)
        return decimal_of(v), decimal_binary(constant)
    return v, constant


# `IN` over a long list (a semi-join's answered values: thousands of them over a large
# class) is answered through an index over the list, built once per list. It is exact:
# inside one declared type `eq` IS set membership -- an int or a string or a bool by
# itself, a double by `orient_key` (which keeps `-0.0` apart from `0.0`), a decimal by
# the converted `Decimal` the scan compares -- and a list that mixes kinds (a cross-type
# constant) keeps the element-by-element path.
_IN_INDEX: dict = {}
_IN_INDEX_MAX = 64
_IN_INDEX_MIN = 8


def _in_index(constant: list, ftype: "str | None", ci: bool):
    cache_key = (id(constant), ftype, ci)
    hit = _IN_INDEX.get(cache_key)
    if hit is not None and hit[0] is constant:
        return hit[1]
    present = [p for p in constant if p is not None]
    index = None
    try:
        if ftype == "decimal":
            if all(_is_number(p) for p in present):
                index = ("decimal", {decimal_binary(p) for p in present})
        else:
            # the constant's conversion for a non-decimal column reads nothing of the
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
    _IN_INDEX[cache_key] = (constant, index)
    return index


def _in_hit(v: Any, constant: list, ftype: "str | None", ci: bool) -> bool:
    """`v IN constant` on a scan: a null element matches nothing."""
    if len(constant) >= _IN_INDEX_MIN:
        index = _in_index(constant, ftype, ci)
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
    return any(p is not None and eq(*_convert(v, p, ftype, ci, "IN")) for p in constant)


def predicate_hit(present: bool, v: Any, op: str, constant: Any, ftype: "str | None" = None, ci: bool = False, key: "str | None" = None) -> bool:
    """What a class SCAN answers (probed 3.2.56) for a column of `ftype`
    (`FieldType.value`; None for an untyped embedded value) with collation `ci`.
    The null rules: a null or absent `v` satisfies `IS NULL` and `<>` and nothing
    else; a null CONSTANT (a bound value that is null, as a value join binds) satisfies
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
        return isinstance(v, dict) and constant in v
    if op == "VALUES HAS":
        return isinstance(v, dict) and any(x is not None and eq(x, constant) for x in v.values())
    if op == "HAS VALUE":
        # the declared LONG values are Long boxes; an Integer-boxed constant never
        # equals one (O7's map face); a Long or a double compares through its box
        if isinstance(constant, int) and not isinstance(constant, bool) and -(2**31) <= constant < 2**31:
            return False
        return isinstance(v, dict) and any(x is not None and eq(x, constant) for x in v.values())
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
        if null_v or constant is None:
            return True
        a, b = _convert(v, constant, ftype, ci, op)
        return not eq(a, b)
    if op in ("<=", ">=") and constant is None:
        return null_v
    if null_v or constant is None:
        return False
    if op == "=":
        a, b = _convert(v, constant, ftype, ci, op)
        return eq(a, b)
    if op == "IN":
        return _in_hit(v, constant, ftype, ci)
    if op == "NOT IN":
        return not _in_hit(v, constant, ftype, ci)

    if op == "BETWEEN":
        lo, hi = constant
        if lo is None or hi is None:
            return False
        a, lo = _convert(v, lo, ftype, ci, op)
        _, hi = _convert(v, hi, ftype, ci, op)
        return not lt(a, lo) and not lt(hi, a)
    if op == "LIKE":
        return isinstance(v, str) and like_hit(v, constant)
    if op == "HAS":
        return isinstance(v, list) and any(eq(x, constant) for x in v)
    if op == "HAS ANY":
        return isinstance(v, list) and any(eq(x, pv) for pv in constant for x in v)
    if op == "HAS ALL":
        # vacuous over an empty constant list (probed: `f CONTAINSALL []` holds for
        # every non-null list)
        return isinstance(v, list) and all(any(eq(x, pv) for x in v) for pv in constant)
    a, b = _convert(v, constant, ftype, ci, op)
    if op == ">":
        return lt(b, a)
    if op == ">=":
        return not lt(a, b)
    if op == "<":
        return lt(a, b)
    if op == "<=":
        return not lt(b, a)
    raise ValueError(op)


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
        return predicate_hit(present, v, "IN", p.constant)
    if p.arith is not None and v is not None and p.key is None:
        v = apply_arith(v, p.arith.sym, p.arith.value, p.ftype)
    return predicate_hit(present, v, p.op, p.constant, p.ftype, p.case_insensitive, p.key)
