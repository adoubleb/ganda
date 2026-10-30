"""How an ArangoDB collection scan answers a predicate over a record: what the generators
steer with and the guards read; no answer is predicted from it."""
from __future__ import annotations

import re
from typing import Any

from ganda.core.query.ir.predicates import Compound
from ganda.arangodb.engine.semantics import apply_arith, eq, lt


# the characters a wildcard does NOT cross: the pattern is an ICU regex whose `.` stops
# at the Unicode line and paragraph separators (probed 2026-10-24: it crosses `\n` and `\r`)
_LINE_TERMINATORS = "\u0085\u2028\u2029"
_WILD = f"[^{_LINE_TERMINATORS}]"


def like_hit(value: Any, pattern: str) -> bool:
    """`value LIKE pattern`: a non-string value is read as its text (null as the empty
    string), `%` any run of characters, `_` one, `\\` escaping the next; case-sensitive."""
    if value is None:
        value = ""
    elif not isinstance(value, str):
        value = str(value).lower() if isinstance(value, bool) else str(value)
    rx = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern):
            rx.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        rx.append(f"{_WILD}*" if c == "%" else _WILD if c == "_" else re.escape(c))
        i += 1
    return re.fullmatch("".join(rx), value) is not None


def _in_hit(v: Any, constant: list) -> bool:
    return any(eq(v, p) for p in constant)


def predicate_hit(present: bool, v: Any, op: str, constant: Any, ftype: "str | None" = None, ci: bool = False, key: "str | None" = None) -> bool:
    """What a collection scan answers for a stored value (absent when not `present`)
    under `op constant`. The collation flag plays no part (no per-attribute collation);
    with `key`, `v` is a map and the comparison reads its entry (a missing map or key
    being null)."""
    if key is not None:
        if op in ("HAS KEY", "VALUES HAS", "HAS VALUE"):
            raise ValueError(op)
        present = isinstance(v, dict) and key in v
        v = v.get(key) if isinstance(v, dict) else None
    if op == "HAS KEY":
        return isinstance(v, dict) and constant in v
    if op in ("VALUES HAS", "HAS VALUE"):
        return isinstance(v, dict) and any(eq(x, constant) for x in v.values())
    if op == "IS NULL":
        return not present or v is None
    if op == "IS NOT NULL":
        return present and v is not None
    if op == "IS PRESENT":
        return present
    if op == "IS ABSENT":
        return not present
    if not present:
        v = None
    if op == "=":
        return eq(v, constant)
    if op == "<>":
        return not eq(v, constant)
    if op == "IN":
        return _in_hit(v, constant)
    if op == "NOT IN":
        return not _in_hit(v, constant)
    if op == "BETWEEN":
        lo, hi = constant
        return not lt(v, lo) and not lt(hi, v)
    if op == "LIKE":
        return like_hit(v, constant)
    if op == "HAS":
        return isinstance(v, list) and any(eq(x, constant) for x in v)
    if op == "HAS ANY":
        return isinstance(v, list) and any(eq(x, p) for p in constant for x in v)
    if op == "HAS ALL":
        return isinstance(v, list) and all(any(eq(x, p) for x in v) for p in constant)
    if op == "<":
        return lt(v, constant)
    if op == "<=":
        return not lt(constant, v)
    if op == ">":
        return lt(constant, v)
    if op == ">=":
        return not lt(v, constant)
    raise ValueError(op)


def evaluate(values: dict, p) -> bool:
    """Two-valued boolean logic over `predicate_hit` (AQL comparison is total, so
    `NOT (f > 5)` holds for a null `f`)."""
    if isinstance(p, Compound):
        if p.op == "NOT":
            return not evaluate(values, p.children[0])
        if p.op == "AND":
            return all(evaluate(values, c) for c in p.children)
        return any(evaluate(values, c) for c in p.children)
    present, v = p.field in values, values.get(p.field)
    if p.sub is not None:
        # the answered values of the same column, compared with `==` (a null answered
        # for a record lacking `g` matches a record lacking `f`)
        return predicate_hit(present, v, "IN", p.constant)
    if p.arith is not None and p.key is None:
        # `(n.f + @c)` over a null or absent f is `0 + c`: a number, present
        v = apply_arith(0 if v is None or isinstance(v, bool) else v, p.arith.sym, p.arith.value, p.ftype) if not isinstance(v, (str, list, dict)) else v
        present = True
    return predicate_hit(present, v, p.op, p.constant, p.ftype, p.case_insensitive, p.key)
