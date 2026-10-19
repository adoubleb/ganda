"""The independent oracle leg: expected answers computed from the `Instance`, under SAP
HANA's value semantics as the three data models' concrete syntaxes ask it. Every rule
is the SQL standard's or SPARQL 1.1's, chosen so that the two agree; `RULES.md` says
which ones the live probe (`ganda hana probe`) checks.

- **Three-valued logic.** A comparison with a null operand is UNKNOWN; `NOT` keeps it
  unknown, `AND` / `OR` follow Kleene's tables, and a record is kept only where the
  whole filter is TRUE. SPARQL's error propagation through `!`, `&&` and `||` is the
  same table (an unbound variable is an error), which is what lets the rdf data model
  ask the same question: a null field is an unbound `OPTIONAL` variable there.
- **Null.** An absent field and an explicit null are the same null on every data model:
  `IS NULL` holds for both; every comparison with them is unknown, `<>` included.
- **The list operators** are two-valued: `CONTAINS`, `CONTAINSANY` and `CONTAINSALL`
  are membership tests (an `EXISTS` over the elements), false over a null list, so
  `NOT (f CONTAINS 5)` holds for one. A whole-list `=` compares the canonical texts
  and is unknown for a null list.
- **A semi-join** `f IN (SELECT g ...)` reads the subquery's non-null values (the
  concrete syntaxes filter `g IS NOT NULL`): unknown for a null `f`, else whether one
  equals it.
- **Order.** Numbers numerically (an integer against a double exactly, as Python
  compares them); strings in UTF-16 code-unit order, which is the binary order of
  HANA's CESU-8 NVARCHAR and equals SPARQL's code-point order on the Basic
  Multilingual Plane -- the provider's value pools hold no astral code point
  (`physical_design.BOUNDARY_VALUES`), the one place the two orders part.
- **Aggregates.** `count` counts the present values; `sum` over integers is exact (an
  answer is emitted only where every partial sum fits a BIGINT); `max` / `min` / `sum`
  over no present value are NULL (SQL; the rdf syntax guards SPARQL's `SUM` of nothing,
  which is 0); `avg` is the exact quotient, read back as an int when it is integral.
- **ORDER BY** puts nulls first ascending and last descending (NULL is the smallest
  value, which is also SPARQL's place for an unbound key); every SQL ordering states it
  with `NULLS FIRST` / `NULLS LAST`.
"""
from __future__ import annotations

import re
from typing import Any

from ganda.core.ast.predicates import Compound
from ganda.core.model import FieldType
from ganda.core.provider import Semantics

UNKNOWN = None


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def hana_key(v: Any):
    """A total-order key reproducing HANA's comparison inside one type, and a stable
    cross-type rank for mixed lists (null < bool < number < string < list)."""
    if v is None:
        return (0,)
    if isinstance(v, bool):
        return (1, int(v))
    if _is_number(v):
        return (2, v)
    if isinstance(v, str):
        return (3, v.encode("utf-16-be", "surrogatepass"))
    if isinstance(v, list):
        return (4, tuple(hana_key(x) for x in v))
    if isinstance(v, dict):
        return (5, tuple(sorted((k, hana_key(x)) for k, x in v.items())))
    return (6, repr(v))


def eq(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return False
    if _is_number(a) and _is_number(b):
        return a == b
    if type(a) is not type(b) and not (_is_number(a) and _is_number(b)):
        return False
    if isinstance(a, list):
        return len(a) == len(b) and all(eq(x, y) for x, y in zip(a, b))
    return a == b


def lt(a: Any, b: Any) -> bool:
    return hana_key(a) < hana_key(b)


def like_hit(value: str, pattern: str) -> bool:
    """SQL LIKE: `%` any run, `_` one character, case-sensitive."""
    rx = "".join(".*" if c == "%" else "." if c == "_" else re.escape(c) for c in pattern)
    return re.fullmatch(rx, value, re.S) is not None


def _cmp(op: str, v: Any, pivot: Any):
    if op == "=":
        return eq(v, pivot)
    if op == "<>":
        return not eq(v, pivot)
    if op == "<":
        return lt(v, pivot)
    if op == "<=":
        return not lt(pivot, v)
    if op == ">":
        return lt(pivot, v)
    if op == ">=":
        return not lt(v, pivot)
    raise ValueError(op)


def truth(present: bool, v: Any, op: str, pivot: Any):
    """A leaf's truth value: True, False or UNKNOWN (None)."""
    null_v = not present or v is None
    if op == "IS NULL":
        return null_v
    if op == "IS NOT NULL":
        return not null_v
    if op == "CONTAINS":
        return isinstance(v, list) and any(eq(x, pivot) for x in v)
    if op == "CONTAINSANY":
        return isinstance(v, list) and any(eq(x, p) for p in pivot for x in v)
    if op == "CONTAINSALL":
        return isinstance(v, list) and all(any(eq(x, p) for x in v) for p in pivot)
    if null_v:
        return UNKNOWN
    if op == "IN":
        if any(p is not None and eq(v, p) for p in pivot):
            return True
        return UNKNOWN if any(p is None for p in pivot) else False
    if op == "NOT IN":
        t = truth(present, v, "IN", pivot)
        return UNKNOWN if t is UNKNOWN else not t
    if op == "BETWEEN":
        lo, hi = pivot
        return _and(truth(present, v, ">=", lo), truth(present, v, "<=", hi))
    if pivot is None:
        return UNKNOWN
    if op == "LIKE":
        return isinstance(v, str) and like_hit(v, pivot)
    return _cmp(op, v, pivot)


def _and(a, b):
    if a is False or b is False:
        return False
    if a is UNKNOWN or b is UNKNOWN:
        return UNKNOWN
    return True


def _or(a, b):
    if a is True or b is True:
        return True
    if a is UNKNOWN or b is UNKNOWN:
        return UNKNOWN
    return False


def _not(a):
    return UNKNOWN if a is UNKNOWN else not a


def predicate_hit(present: bool, v: Any, op: str, pivot: Any, ftype: "str | None" = None, ci: bool = False, key: "str | None" = None) -> bool:
    """Whether a stored value satisfies `op pivot` (TRUE, not unknown)."""
    if key is not None:
        raise ValueError("the SAP HANA provider has no map fields")
    return truth(present, v, op, pivot) is True


def apply_arith(v, sym: str, c, ftype: "str | None"):
    """`v <sym> c` in the column's arithmetic (the callers keep it exact)."""
    return {"+": v + c, "-": v - c, "*": v * c}[sym]


def tri(values: dict, p):
    """A predicate tree's truth value over a record."""
    if isinstance(p, Compound):
        if p.op == "NOT":
            return _not(tri(values, p.children[0]))
        out = tri(values, p.children[0])
        for c in p.children[1:]:
            out = (_and if p.op == "AND" else _or)(out, tri(values, c))
        return out
    present, v = p.field in values, values.get(p.field)
    if p.sub is not None:
        if not present or v is None:
            return UNKNOWN
        return any(x is not None and eq(v, x) for x in p.pivot)
    if p.arith is not None and v is not None:
        v = apply_arith(v, p.arith.sym, p.arith.value, p.ftype)
    return truth(present, v, p.op, p.pivot)


def evaluate(values: dict, p) -> bool:
    """Whether a record is kept by the filter: the tree is TRUE."""
    return tri(values, p) is True


def mean(values: list):
    """AVG as HANA SQL answers it: over integers a DECIMAL(25,6) (documented), the exact
    quotient TRUNCATED to six places (7/128 is 0.054687, probe R20) -- read back as an
    int when integral; over doubles the double quotient. The triple store's AVG is
    double(SUM) / COUNT instead (B13, probe K23): an rdf average is compared only where
    the two agree (`known_bugs`)."""
    total, n = sum(values), len(values)
    if all(isinstance(v, int) and not isinstance(v, bool) for v in values):
        if total % n == 0:
            return total // n
        from decimal import ROUND_DOWN, Decimal

        q = (Decimal(total) / Decimal(n)).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        return int(q) if q == q.to_integral_value() else float(q)
    return total / n


I64 = 2**63


def sum_is_exact(values: list, ftype: "str | None" = "int") -> bool:
    """Integers whose absolute total stays below 2^62, so no order of the partial sums
    leaves a BIGINT; a double sum depends on the order, so it is never exact."""
    if any(isinstance(v, bool) or not isinstance(v, int) for v in values):
        return False
    return sum(abs(v) for v in values) < 2**62


def agg(fn: str, values: list, ftype: "str | None" = None):
    present = [v for v in values if v is not None]
    if fn == "count":
        return len(present)
    if fn == "distinct_count":
        return len({hana_key(v) for v in present})
    if not present:
        return None
    if fn == "sum":
        return sum(present)
    if fn == "max":
        return max(present, key=hana_key)
    if fn == "min":
        return min(present, key=hana_key)
    raise ValueError(fn)


def order_key(v, desc: bool):
    """A per-key sort key: nulls first ascending, last descending; the key order
    reversed for DESC (numbers and booleans, the types an ordering of several keys
    names)."""
    if v is None:
        return (1 if desc else 0, ())
    k = hana_key(v)
    if desc:
        if k[0] in (1, 2):
            return (0, (k[0], -k[1]))
        raise ValueError(f"a DESC key over {v!r}")
    return (1, k)


class HanaSemantics(Semantics):
    key = staticmethod(hana_key)
    eq = staticmethod(eq)
    lt = staticmethod(lt)
    predicate_hit = staticmethod(predicate_hit)
    evaluate = staticmethod(evaluate)
    apply_arith = staticmethod(apply_arith)
    agg = staticmethod(agg)
    mean = staticmethod(mean)
    sum_is_exact = staticmethod(sum_is_exact)
    order_key = staticmethod(order_key)

    def aggregate_over_no_rows(self, fn: str) -> list:
        # an aggregate without GROUP BY answers one row over no rows: NULL, a distinct
        # count 0; `set` / `list` are the rows of the present values, so none
        if fn in ("set", "list"):
            return []
        if fn == "distinct_count":
            return [{"v": 0}]
        return [{"v": None}]

    def nulls_first(self, desc: bool) -> bool:
        return not desc

    def orders_exactly(self, ftype, custom_keys: bool) -> bool:
        return ftype is not FieldType.LIST


SEMANTICS = HanaSemantics()
