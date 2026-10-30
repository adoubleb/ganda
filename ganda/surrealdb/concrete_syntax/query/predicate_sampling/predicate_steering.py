"""How a SurrealDB table scan answers a predicate over a record (`predicate_hit`, `evaluate`), for steering and guards.
NONE and NULL sort below every value; arithmetic over a missing or overflowing value answers false."""
from __future__ import annotations

from typing import Any

from ganda.core.constants import I64_MAX, I64_MIN
from ganda.core.query.ir.predicates import Compound
from ganda.surrealdb.engine.semantics import _is_number, apply_arith, eq, lt
from ganda.surrealdb.rules import assumes
from ganda.surrealdb.rules.catalog import SURQL

_CI_OPS = ("=", "<>", "<", "<=", ">", ">=", "IN")

assumes("compare.none_and_null.below_every_value", SURQL, True)


def like_hit(value: str, pattern: str, ci: bool = False) -> bool:
    """The pattern's shape as the text asks it: a prefix (`v%`), a suffix (`%v`), an
    infix (`%v%`) or the value itself."""
    if ci:
        value, pattern = value.lower(), pattern.lower()
    lead, trail = pattern.startswith("%"), pattern.endswith("%") and len(pattern) > 1
    body = pattern.strip("%")
    if lead and trail:
        return body in value
    if lead:
        return value.endswith(body)
    if trail:
        return value.startswith(body)
    return value == pattern


def _convert(v: Any, constant: Any, ci: bool, op: str):
    if ci and isinstance(v, str) and isinstance(constant, str) and op in _CI_OPS:
        return v.lower(), constant.lower()
    return v, constant


def _in_hit(v: Any, constant: list, ci: bool) -> bool:
    return any(p is not None and eq(*_convert(v, p, ci, "IN")) for p in constant)


def predicate_hit(present: bool, v: Any, op: str, constant: Any, ftype: "str | None" = None, ci: bool = False, key: "str | None" = None) -> bool:
    if key is not None:
        if op in ("HAS KEY", "VALUES HAS", "HAS VALUE"):
            raise ValueError(op)
        present = isinstance(v, dict) and key in v
        v = v.get(key) if isinstance(v, dict) else None
    if op == "HAS KEY":
        return isinstance(v, dict) and constant in v
    if op in ("VALUES HAS", "HAS VALUE"):
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
        return not eq(*_convert(v, constant, ci, op))
    if constant is None:
        # a missing constant (a correlated value) sorts below every value
        return (op == ">=") or (op == ">" and not null_v) or (op == "<=" and null_v)
    if null_v:
        # NONE and NULL sort below every value
        return op in ("<", "<=")
    if op == "=":
        return eq(*_convert(v, constant, ci, op))
    if op == "IN":
        return _in_hit(v, constant, ci)
    if op == "NOT IN":
        return not _in_hit(v, constant, ci)
    if op == "BETWEEN":
        lo, hi = constant
        if lo is None or hi is None:
            return False
        a, lo = _convert(v, lo, ci, op)
        _, hi = _convert(v, hi, ci, op)
        return not lt(a, lo) and not lt(hi, a)
    if op == "LIKE":
        return isinstance(v, str) and like_hit(v, constant, ci)
    if op == "HAS":
        return isinstance(v, list) and any(eq(x, constant) for x in v)
    if op == "HAS ANY":
        return isinstance(v, list) and any(eq(x, pv) for pv in constant for x in v)
    if op == "HAS ALL":
        return isinstance(v, list) and all(any(eq(x, pv) for x in v) for pv in constant)
    a, b = _convert(v, constant, ci, op)
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
    """Two-valued boolean logic over `predicate_hit` for the leaves (`NOT (f > 5)`
    holds for a missing `f`); an arithmetic leaf over a missing value is false (the
    text guards the arithmetic the engine would refuse)."""
    if isinstance(p, Compound):
        if p.op == "NOT":
            return not evaluate(values, p.children[0])
        if p.op == "AND":
            return all(evaluate(values, c) for c in p.children)
        return any(evaluate(values, c) for c in p.children)
    present, v = p.field in values, values.get(p.field)
    if p.sub is not None:
        return predicate_hit(present, v, "IN", p.constant)
    if p.arith is not None and p.key is None:
        if not present or v is None or not _is_number(v):
            return False
        v = apply_arith(v, p.arith.sym, p.arith.value, p.ftype)
        if p.ftype in ("int", "int32") and isinstance(v, int) and not I64_MIN <= v <= I64_MAX:
            return False  # the text guards the arithmetic the engine would refuse
    return predicate_hit(present, v, p.op, p.constant, p.ftype, p.case_insensitive, p.key)
