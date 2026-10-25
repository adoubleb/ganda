"""AQL's value semantics (the legacy port probed them on 3.12; `ganda arangodb probe`
re-checks the ones listed in `RULES.md`): the value order and equality the oracle and
the state check compare answers under, and the predicate logic and arithmetic the
generators walk the `Instance` with. No answer is predicted from them.

- **Order across types** is total: null < bool < number < string < array < object. So
  a comparison never fails for an absent or null value: `null < 5` and `null <= 5`
  hold, `null > 5` does not, `null != 5` holds, `null == null` holds. An absent
  attribute reads as null; `HAS()` tells them apart.
- **Numbers** are one type: `1 == 1.0`; a 64-bit integer compared with a double is
  compared EXACTLY (`2^53 + 1 == 9007199254740992.0` is false, probed V2). A stored `-0.0` is one value to comparison and two to hashing
  (A1), which is why the generator stores none.
- **Strings** compare for equality code point by code point (no Unicode
  normalization); their ORDER is ICU collation, which Python does not reproduce, so
  the generators' filters never hold an ordered comparison on a string column
  (`pivots.ordered_ops`); `key` orders strings by code point for the oracle's own
  canonical forms only. The model's DATETIME / DATE values are strings of one fixed
  shape, on which ICU and code-point order agree digit by digit.
- **`LIKE`** is case-sensitive, `%` any run and `_` one character, across a newline
  too but not across the Unicode line and paragraph separators (the pattern is an ICU
  regex: probed V5); the generator never builds a pattern from a value holding a
  wildcard or a line terminator.
- **`IN`** compares with `==`, so a null element matches a null value (a semi-join's
  answered nulls do match the records lacking the field).
- **Aggregates**: `MAX` / `MIN` skip null and answer null over nothing; `SUM` reads
  null as 0 and answers 0 over nothing; `COUNT_DISTINCT` over nothing is 0; `AVERAGE`
  and `MEDIAN` over nothing are null. Sums run in double: a sum is exact only over
  integers whose absolute total stays under 2^53.
"""
from __future__ import annotations

import re
from typing import Any

from ganda.core.query.ir.predicates import Compound
from ganda.core.engine.provider import Semantics

F64_INT_EXACT = 2**53


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
        # (probed V2: `2^53 + 1 == 9007199254740992.0` is false)
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


# the characters a wildcard does NOT cross: the pattern is an ICU regex whose `.` stops
# at the Unicode line and paragraph separators (probed V5: it crosses `\n` and `\r`)
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


def _in_hit(v: Any, pivot: list) -> bool:
    return any(eq(v, p) for p in pivot)


def predicate_hit(present: bool, v: Any, op: str, pivot: Any, ftype: "str | None" = None, ci: bool = False, key: "str | None" = None) -> bool:
    """What a collection scan answers for a stored value (absent when not `present`)
    under `op pivot`. The collation flag plays no part (no per-attribute collation);
    with `key`, `v` is a map and the comparison reads its entry (a missing map or key
    being null)."""
    if key is not None:
        if op in ("HAS KEY", "VALUES HAS", "HAS VALUE"):
            raise ValueError(op)
        present = isinstance(v, dict) and key in v
        v = v.get(key) if isinstance(v, dict) else None
    if op == "HAS KEY":
        return isinstance(v, dict) and pivot in v
    if op in ("VALUES HAS", "HAS VALUE"):
        return isinstance(v, dict) and any(eq(x, pivot) for x in v.values())
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
        return eq(v, pivot)
    if op == "<>":
        return not eq(v, pivot)
    if op == "IN":
        return _in_hit(v, pivot)
    if op == "NOT IN":
        return not _in_hit(v, pivot)
    if op == "BETWEEN":
        lo, hi = pivot
        return not lt(v, lo) and not lt(hi, v)
    if op == "LIKE":
        return like_hit(v, pivot)
    if op == "HAS":
        return isinstance(v, list) and any(eq(x, pivot) for x in v)
    if op == "HAS ANY":
        return isinstance(v, list) and any(eq(x, p) for p in pivot for x in v)
    if op == "HAS ALL":
        return isinstance(v, list) and all(any(eq(x, p) for x in v) for p in pivot)
    if op == "<":
        return lt(v, pivot)
    if op == "<=":
        return not lt(pivot, v)
    if op == ">":
        return lt(pivot, v)
    if op == ">=":
        return not lt(v, pivot)
    raise ValueError(op)


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
        return predicate_hit(present, v, "IN", p.pivot)
    if p.arith is not None and v is not None and p.key is None:
        v = apply_arith(v, p.arith.sym, p.arith.value, p.ftype)
    return predicate_hit(present, v, p.op, p.pivot, p.ftype, p.case_insensitive, p.key)


def canonical(value):
    if isinstance(value, float) and value.is_integer() and abs(value) < 2**63:
        return int(value)
    if isinstance(value, list):
        return [canonical(v) for v in value]
    if isinstance(value, dict):
        return {k: canonical(v) for k, v in value.items()}
    return value


class ArangoSemantics(Semantics):
    @staticmethod
    def arith_exact(v, sym, c, ftype) -> bool:
        from ganda.arangodb.oracle.rewrites import arith_exact

        return arith_exact(v, sym, c, ftype)

    key = staticmethod(aql_key)
    eq = staticmethod(eq)
    lt = staticmethod(lt)
    predicate_hit = staticmethod(predicate_hit)
    evaluate = staticmethod(evaluate)
    apply_arith = staticmethod(apply_arith)
    sum_is_exact = staticmethod(sum_is_exact)

    def canonical(self, value):
        """The wire form: an integral double comes back as an integer (`2.0` is `2`)."""
        return canonical(value)


SEMANTICS = ArangoSemantics()
