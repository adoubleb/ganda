"""Predicate drawing and evaluation, shared by the IRs (the OrientDB text is
`predicate_syntax.render_predicate`).

A predicate is a tree: a `Predicate` LEAF is `(op, pivot)` over one field; a `Compound`
joins children with `AND` / `OR` or negates one with `NOT`. The pivot of a leaf sits ON
a stored value, ADJACENT to one, or OUTSIDE the range (the SurrealDB/ArangoDB
discipline: bugs live at boundaries). Every op has an exact Python mirror in
`semantics.predicate_hit`, and `evaluate` folds the tree with OrientDB's two-valued
boolean logic (probed 3.2.56: `NOT (f > 5)` holds for a null `f`, `a OR b AND c`
binds AND first -- every rendered compound is parenthesized anyway).

Two adversarial axes a leaf can carry:

- a CROSS-TYPE numeric pivot (`cross_type_probability`): a double pivot on a LONG
  column (`5.5`, `5.0`) or an integral pivot on a DOUBLE column. OrientDB compares
  the two through double (probed: `2^53 + 1 = 9007199254740992.0`), which the
  reference reproduces (`semantics.lt`/`eq` convert), and the index-vs-scan oracle
  says whether an index lookup agrees with that comparison;
- a NULL pivot never arises from drawing (the null rules are asked through the
  `IS NULL` query generator), but the value-join query generator binds a record's own field, which may
  be null; `semantics.predicate_hit` models what a scan answers for it.

A leaf's `bind` is a bare parameter NAME (`p1`); the engine's parameter syntax is
the renderer's. A SEMI-JOIN leaf (`INSUB`, the select IR only) asks whether `f` is
among the `g` values of `t2`'s records matching `q`: its pivot is
the list of `g` values the subquery answers over the Instance it was drawn over
(`resolve_subs` recomputes it over another), and it binds only `q`'s pivots. A `LIKE` pattern is built
from a stored value that holds no `%`, `_` or `\\`, so `like_hit` is exact; `IS NULL`
/ `IS NOT NULL` / `IS DEFINED` / `IS NOT DEFINED` bind nothing.
"""
from __future__ import annotations

import dataclasses
import math
import random
import struct
from dataclasses import dataclass
from typing import Any

from ganda.orientdb.model import DATE_TYPES, INT_TYPES, MAP_KEYS, NUMERIC_TYPES, FieldSpec, FieldType

I64_MAX = 2**63 - 1
I64_MIN = -(2**63)
I32_MAX = 2**31 - 1
I32_MIN = -(2**31)

_ORDERED_OPS = ("=", "<>", ">", ">=", "<", "<=", "IN", "BETWEEN")
_EQUALITY_OPS = ("=", "<>", "IN")
_NULL_OPS = ("IS NULL", "IS NOT NULL", "IS DEFINED", "IS NOT DEFINED")
NO_BIND_OPS = _NULL_OPS
# the map operators: `m CONTAINSKEY :k`, `m.values() CONTAINS :p`, `m CONTAINSVALUE :p`
MAP_OPS = ("CONTAINSKEY", "VALUES CONTAINS", "CONTAINSVALUE")
COMPOUND_OPS = ("AND", "OR", "NOT")

COMPOUND_PROBABILITY = 0.3
CROSS_TYPE_PROBABILITY = 0.2


@dataclass(frozen=True)
class Predicate:
    """A leaf."""

    on: str  # "source" | "edge" | "target"
    field: str
    op: str
    bind: str  # the bind-variable name carrying the pivot ("" for a null op)
    pivot: Any
    reference: bool = True
    ftype: "str | None" = None  # the column's `FieldType.value`; None for an untyped embedded value
    ci: bool = False  # the column is `COLLATE ci`
    key: "str | None" = None  # a map entry: the leaf compares `field.key` (or `field[:kb]` when `key_bind`)
    key_bind: str = ""
    # REVERSED operands: the pivot on the left (`:p > f` for `f < :p`, `:p IN f` for
    # `f CONTAINS :p`) -- the same comparison the planner never serves from an index
    # (probed: every reversed leaf is a scan), so a `<data_model>:rev` variant is the
    # index-vs-scan oracle inside one database. With a STRING pivot on a DATETIME /
    # DATE column the engine compares the date's Java `toString()` (O32, withheld).
    rev: bool = False
    # ARITHMETIC over the field: `(f <sym> :cbind) op :bind` where the pivot is
    # already shifted (`p + c`), an equivalent concrete syntax wherever the arithmetic is
    # exact on every stored value (`arithmetic()` guards that) -- also never
    # index-served. (sym, cbind, c).
    arith: "tuple | None" = None
    # a SEMI-JOIN (`op == "INSUB"`): `f IN (SELECT g FROM t2 [WHERE q])` as `(t2, g,
    # q, scan)`, `scan` reading the subquery over `(SELECT FROM t2)` (never
    # index-served); the pivot is the answered `g` list, nulls included
    sub: "tuple | None" = None

    def to_dict(self) -> dict:
        d = {"on": self.on, "field": self.field, "op": self.op, "bind": self.bind, "pivot": self.pivot, "reference": self.reference, "ftype": self.ftype, "ci": self.ci, "key": self.key, "key_bind": self.key_bind}
        if self.rev:
            d["rev"] = True
        if self.arith is not None:
            d["arith"] = list(self.arith)
        if self.sub is not None:
            t2, g, q, scan = self.sub
            d["sub"] = {"table": t2, "field": g, "pred": q.to_dict() if q is not None else None, "scan": scan}
        return d

    @staticmethod
    def from_dict(d: "dict | None"):
        if d is None:
            return None
        if d.get("kind") == "compound":
            return Compound(d["on"], d["op"], tuple(Predicate.from_dict(c) for c in d["children"]))
        arith = tuple(d["arith"]) if d.get("arith") else None
        sub = None
        if d.get("sub"):
            x = d["sub"]
            sub = (x["table"], x["field"], Predicate.from_dict(x["pred"]), x["scan"])
        return Predicate(d["on"], d["field"], d["op"], d["bind"], d["pivot"], d.get("reference", True), d.get("ftype"), d.get("ci", False), d.get("key"), d.get("key_bind", ""), d.get("rev", False), arith, sub)

    @property
    def bind_vars(self) -> dict:
        if self.sub is not None:
            return dict(self.sub[2].bind_vars) if self.sub[2] is not None else {}
        out = {self.key_bind: self.key} if self.key_bind else {}
        if self.arith is not None:
            out[self.arith[1]] = self.arith[2]
        if self.op in NO_BIND_OPS:
            return out
        if self.op == "BETWEEN":
            out.update({self.bind + "a": self.pivot[0], self.bind + "b": self.pivot[1]})
        else:
            out[self.bind] = self.pivot
        return out

    def leaves(self) -> list:
        return [self]

    @property
    def signature(self) -> str:
        if self.sub is not None:
            return f"INSUB({self.sub[2].signature if self.sub[2] is not None else '-'}{'@scan' if self.sub[3] else ''})"
        return f"{self.op}@key" if self.key is not None else self.op

    @property
    def plain(self) -> bool:
        """Neither reversed nor arithmetic: the form an index can serve."""
        return not self.rev and self.arith is None


@dataclass(frozen=True)
class Compound:
    on: str
    op: str  # AND | OR | NOT
    children: tuple

    def to_dict(self) -> dict:
        return {"kind": "compound", "on": self.on, "op": self.op, "children": [c.to_dict() for c in self.children]}

    @property
    def bind_vars(self) -> dict:
        out: dict = {}
        for c in self.children:
            out.update(c.bind_vars)
        return out

    @property
    def reference(self) -> bool:
        return all(c.reference for c in self.children)

    def leaves(self) -> list:
        return [leaf for c in self.children for leaf in c.leaves()]

    @property
    def signature(self) -> str:
        return f"{self.op}({','.join(c.signature for c in self.children)})"


def leaves(p) -> list:
    return [] if p is None else p.leaves()


def fields_of(p) -> list:
    """The distinct field names a predicate tree touches, in first-seen order."""
    out: list = []
    for leaf in leaves(p):
        if leaf.field not in out:
            out.append(leaf.field)
    return out


def has_op(p, ops) -> bool:
    return any(leaf.op in ops for leaf in leaves(p))


def subqueries(p) -> list:
    """The semi-join leaves of a tree (`INSUB`)."""
    return [leaf for leaf in leaves(p) if leaf.sub is not None]


def has_op_deep(p, ops) -> bool:
    """`has_op` looking into the semi-joins' own predicates too."""
    return has_op(p, ops) or any(leaf.sub[2] is not None and has_op_deep(leaf.sub[2], ops) for leaf in subqueries(p))


def sub_values(instance, table: str, field_name: str, q) -> list:
    """The `g` values `SELECT g FROM t2 [WHERE q]` answers: one per record, a null
    for an absent or null `g`."""
    rows = instance.nodes_of(table)
    return [n.values.get(field_name) for n in rows if q is None or evaluate(n.values, q)]


def resolve_subs(p, instance):
    """`p` with every semi-join's pivot recomputed over `instance` (a tree drawn over
    one Instance evaluated over another: the transaction door's)."""
    if p is None:
        return None
    if isinstance(p, Compound):
        return Compound(p.on, p.op, tuple(resolve_subs(c, instance) for c in p.children))
    if p.sub is None:
        return p
    t2, g, q, _scan = p.sub
    return dataclasses.replace(p, pivot=sub_values(instance, t2, g, resolve_subs(q, instance)))


def evaluate(values: dict, p) -> bool:
    """Two-valued boolean logic over `semantics.predicate_hit` for the leaves."""
    from ganda.orientdb.semantics import predicate_hit

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
        v = apply_arith(v, p.arith[0], p.arith[2], p.ftype)
    return predicate_hit(present, v, p.op, p.pivot, p.ftype, p.ci, p.key)


# --- reversed operands and arithmetic -------------------------------------------------------

MIRROR = {"=": "=", "<>": "<>", "<": ">", ">": "<", "<=": ">=", ">=": "<="}
_REVERSIBLE = tuple(MIRROR) + ("CONTAINS",)


def reverse(p):
    """`p` with every reversible leaf's operands swapped (`:p > f`), or None when no
    leaf is reversible (IN, BETWEEN, LIKE, the null tests and the map operators keep
    their concrete syntax). A leaf that already carries arithmetic is left alone."""
    if isinstance(p, Compound):
        kids = [reverse(c) for c in p.children]
        if all(k is None for k in kids):
            return None
        return Compound(p.on, p.op, tuple(k if k is not None else c for k, c in zip(kids, p.children)))
    if p.op not in _REVERSIBLE or p.rev or p.arith is not None:
        return None
    return dataclasses.replace(p, rev=True)


def apply_arith(v, sym: str, c, ftype: "str | None"):
    """`v <sym> c` as the engine computes it where the arithmetic is exact (the
    caller guarded that): integer arithmetic on the integer types, double / float
    arithmetic (exact by construction) on the floating ones, exact decimal
    arithmetic on DECIMAL, a millisecond shift on the date types."""
    if ftype in ("datetime", "date"):
        return _shift_date(v, (c if sym == "+" else -c) // 1000)
    if ftype == "decimal":
        from decimal import Decimal

        a = Decimal(repr(v)) if isinstance(v, float) else Decimal(v)
        r = {"+": a + c, "-": a - c, "*": a * c}[sym]
        return float(r)
    return {"+": v + c, "-": v - c, "*": v * c}[sym]


def _arith_exact(v, sym: str, c, ftype: FieldType) -> bool:
    """Whether `v <sym> c` is computed exactly in the column's Java box: an int
    (INTEGER `*` wraps in 32 bits, `+` / `-` wrap on a negative overflow, O29),
    a long, a float32-exact float, a double scaled by a power of two, a decimal
    with at most three places, a date inside the format's range."""
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
        return math.isfinite(r) and _f32(r) == r and abs(r) < 2**24
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


_ARITH_CHOICES = {
    FieldType.INT: (("+", 1), ("+", 7), ("-", 1), ("-", 3), ("*", 2), ("*", 3), ("*", -1), ("+", -2)),
    FieldType.INT32: (("+", 1), ("+", 7), ("-", 1), ("-", 3), ("*", 2), ("*", 3), ("*", -1), ("+", -2)),
    FieldType.FLOAT32: (("+", 1), ("-", 1), ("*", 2), ("*", 3), ("*", -1)),
    # a double sum rounds (adjacent doubles collapse under `+ 1`, so `f + 1 > p + 1`
    # is not `f > p`): only exact scalings by a power of two
    FieldType.FLOAT: (("*", 2), ("*", -1), ("*", 4)),
    FieldType.DECIMAL: (("+", 1), ("-", 1), ("*", 2), ("*", 3), ("*", -1), ("+", 100)),
    FieldType.DATETIME: (("+", 1000), ("-", 1000), ("+", 86400000), ("-", 3600000), ("+", 60000)),
    FieldType.DATE: (("+", 1000), ("+", 86400000), ("-", 86400000), ("-", 1000)),
}


def arithmetic(p, rng: random.Random, bind: str, ftype: FieldType, values: list):
    """An equivalent concrete syntax of the leaf `f op :p` as `(f <sym> :c) op' :pc AND f IS
    NOT NULL` (`... OR f IS NULL` for `<>`) -- the pivot shifted the same way, the
    operator mirrored under a negative factor, the null rows handled explicitly (a
    null operand is the identity of `+` and `-`, O30: `null + 1 = 1` would match;
    and `null <> p` holds). None unless the
    arithmetic is exact on every stored value and on the pivot, the pivot is of the
    column's own kind, and the column is not `COLLATE ci`."""
    if not isinstance(p, Predicate) or p.op not in MIRROR or p.key is not None or p.ci or not p.plain or ftype not in _ARITH_CHOICES:
        return None
    pv = p.pivot
    if isinstance(pv, bool) or pv is None:
        return None
    if ftype in (FieldType.INT, FieldType.INT32):
        if not isinstance(pv, int):
            return None
    elif ftype in (FieldType.FLOAT, FieldType.FLOAT32, FieldType.DECIMAL):
        if not isinstance(pv, float) or lossy_pivot(ftype, pv):
            return None
    elif ftype in DATE_TYPES:
        if not isinstance(pv, str) or len(pv) != 19:
            return None
    sym, c = rng.choice(_ARITH_CHOICES[ftype])
    if not all(_arith_exact(v, sym, c, ftype) for v in values) or not _arith_exact(pv, sym, c, ftype):
        return None
    if sym in ("+", "-") and ftype in (FieldType.FLOAT, FieldType.FLOAT32, FieldType.DECIMAL):
        # a sum erases the sign of zero, which the comparison keeps (`-0.0 < 0.0`,
        # Double.compare): with both zeros among the values and the pivot, `f = -0.0`
        # misses a stored 0.0 where `f - 1 = -1.0` matches it (cov400 seed 5087)
        signs = {math.copysign(1.0, float(v)) for v in list(values) + [pv] if isinstance(v, (int, float)) and not isinstance(v, bool) and float(v) == 0}
        if len(signs) > 1:
            return None
    op = p.op if (c > 0 or sym != "*") else MIRROR[p.op]
    cbind = f"{bind}c"
    leaf = Predicate(p.on, p.field, op, bind, apply_arith(pv, sym, c, ftype.value), p.reference, p.ftype, p.ci, None, "", False, (sym, cbind, c))
    if op == "<>":
        # `f <> p` holds for a null f: the null rows are added back explicitly
        return Compound(p.on, "OR", (leaf, Predicate(p.on, p.field, "IS NULL", "", None, p.reference, p.ftype, p.ci)))
    return Compound(p.on, "AND", (leaf, Predicate(p.on, p.field, "IS NOT NULL", "", None, p.reference, p.ftype, p.ci)))


def _f32(v: float) -> float:
    try:
        return struct.unpack("f", struct.pack("f", v))[0]
    except OverflowError:
        return v


def lossy_pivot(ftype: FieldType, v) -> bool:
    """Whether converting `v` to the column's type changes it -- the pivots an index
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
        from decimal import Decimal

        return Decimal(v) != Decimal(repr(v))
    if ftype in DATE_TYPES:
        # a string the format only partially matches: the engine compares it as the
        # date's midnight (O27); a date-only string or an epoch integer converts
        # faithfully
        return isinstance(v, str) and ("T" in v or (len(v) == 16 and v[10] == " "))
    return False


def _shift_date(v: str, seconds: int) -> str:
    import datetime as _dt

    try:
        t = _dt.datetime.strptime(v, "%Y-%m-%d %H:%M:%S") + _dt.timedelta(seconds=seconds)
    except (ValueError, OverflowError):
        return v
    if t.year < 1 or t.year > 9999:
        return v
    return t.strftime("%Y-%m-%d %H:%M:%S")


def _offset(ftype: FieldType, v, direction: int):
    if ftype is FieldType.INT:
        nxt = v + direction
        return nxt if I64_MIN <= nxt <= I64_MAX else v
    if ftype is FieldType.INT32:
        nxt = v + direction
        return nxt if I32_MIN <= nxt <= I32_MAX else v
    if ftype is FieldType.FLOAT:
        nxt = math.nextafter(v, math.inf if direction > 0 else -math.inf)
        return nxt if math.isfinite(nxt) else v
    if ftype is FieldType.FLOAT32:
        # the neighbouring float32, widened (sent as an exact double)
        bits = struct.unpack("i", struct.pack("f", v))[0]
        if v == 0:
            nxt = _f32(math.nextafter(0.0, math.inf if direction > 0 else -math.inf))
            return nxt
        step = direction if v > 0 else -direction
        try:
            nxt = struct.unpack("f", struct.pack("i", bits + step))[0]
        except (struct.error, OverflowError):
            return v
        return nxt if math.isfinite(nxt) else v
    if ftype is FieldType.DECIMAL:
        return round(v + 0.001 * direction, 3)
    if ftype is FieldType.DATETIME:
        return _shift_date(v, direction)
    if ftype is FieldType.DATE:
        return _shift_date(v, 86400 * direction)
    if ftype is FieldType.STRING:
        # the code-unit successor; the predecessor of a non-empty string is its
        # longest proper prefix (a strictly smaller string in every order)
        return v + chr(0) if direction > 0 else (v[:-1] if v else v)
    return v


def _pattern_safe(v: str) -> bool:
    return v != "" and not any(c in v for c in "%?_\\")


def _cross_type(rng: random.Random, ftype: FieldType, v):
    """The same pivot rendered in a form the column does not hold exactly: a LONG
    pivot as an exact double (`5.0`) or a half-step off it (`5.5`, where an index key
    conversion that truncates would bite); an INTEGER pivot as that, or as a Long
    that wraps onto it (`v + 2^32`) or lies just outside int32; a DOUBLE pivot as
    the integer it truncates to; a FLOAT pivot as its integer or as the next double
    (not float32-exact, so an index rounds it back onto the value); a DECIMAL pivot
    as its integer or left as the stored value (non-dyadic: a scan then compares its
    binary expansion, O20)."""
    if ftype is FieldType.INT:
        if abs(v) >= 2**52:
            return float(v)
        return float(v) if rng.random() < 0.4 else v + rng.choice((0.5, -0.5))
    if ftype is FieldType.INT32:
        roll = rng.random()
        if roll < 0.3:
            return float(v)
        if roll < 0.5:
            return v + rng.choice((0.5, -0.5))
        if roll < 0.8:
            return v + rng.choice((2**32, -(2**32)))
        return rng.choice((2**31, -(2**31) - 1, 2**40))
    if ftype is FieldType.FLOAT:
        if not math.isfinite(v) or abs(v) >= 2**62:
            return v
        return int(v) if rng.random() < 0.7 else int(v) + rng.choice((1, -1))
    if ftype is FieldType.FLOAT32:
        roll = rng.random()
        if roll < 0.4:
            return int(v) if abs(v) < 2**62 else v
        if roll < 0.6:
            return 2**24 + rng.choice((1, -1, 3))
        nxt = math.nextafter(v, math.inf if rng.random() < 0.5 else -math.inf)
        return nxt if math.isfinite(nxt) else v
    if ftype is FieldType.DECIMAL:
        return int(v) if abs(v) < 2**62 else v
    if ftype in DATE_TYPES:
        import datetime as _dt

        roll = rng.random()
        if roll < 0.35:
            return v[:10]  # the date alone: midnight
        if roll < 0.7:
            t = _dt.datetime.strptime(v, "%Y-%m-%d %H:%M:%S")
            return int((t - _dt.datetime(1970, 1, 1)).total_seconds()) * 1000 + rng.choice((0, 0, 500))
        if roll < 0.85:
            return v[:10] + "T" + v[11:]  # the ISO separator: compared as midnight (O27)
        return v[:16]  # no seconds: compared as midnight (O27)
    return v


def _decimal_pivot(rng: random.Random, v):
    """A DECIMAL pivot: half the time the stored value itself (a non-dyadic double,
    which a scan compares through its binary expansion, O20), otherwise the nearest
    dyadic multiple of 1/8 (exact in both representations)."""
    if isinstance(v, float) and math.isfinite(v) and rng.random() < 0.5:
        return round(v * 8) / 8
    return v


def draw(rng: random.Random, on: str, f: FieldSpec, values: list, bind: str, cross_type_probability: float = CROSS_TYPE_PROBABILITY) -> "Predicate | None":
    """A LEAF predicate on `f` whose stored (non-null) values are `values`. None when
    the column holds nothing to aim at."""
    present = [v for v in values if v is not None]
    ft, ci = f.type.value, f.ci

    def leaf(op, pivot):
        return Predicate(on, f.name, op, "" if op in NO_BIND_OPS else bind, pivot, True, ft, ci)

    roll = rng.random()
    if roll < 0.12:
        return leaf(rng.choice(_NULL_OPS), None)
    if not present:
        return None
    if f.type is FieldType.LIST:
        # a list equality against a declared list never matches (O7, withheld on
        # every declared data model), so CONTAINS carries most of the weight; CONTAINSANY /
        # CONTAINSALL take a list of one or two elements (an Integer-boxed element
        # never equals a stored Long on a scan: O7's list face, withheld there)
        roll = rng.random()
        if roll < 0.2:
            v = rng.choice(present)
            return leaf("=", list(v))
        elems = [x for v in present for x in v]
        if not elems:
            return None
        if roll < 0.45:
            k = rng.randint(1, min(2, len(elems)))
            pivots = [rng.choice(elems) for _ in range(k)]
            if rng.random() < 0.3:
                # a pivot just past a stored element -- inside the long range: 2^63
                # is no LONG, and the server cannot even unmarshal the parameter
                # (write-IR smoke w-smoke4, seed 1124)
                pivots[-1] = pivots[-1] + 1 if pivots[-1] < I64_MAX else pivots[-1] - 1
            return leaf(rng.choice(("CONTAINSANY", "CONTAINSALL")), pivots)
        return leaf("CONTAINS", rng.choice(elems))
    if f.type is FieldType.MAP:
        return _draw_map(rng, on, f, present, bind, leaf)
    if f.type is FieldType.BOOL:
        return leaf(rng.choice(_EQUALITY_OPS[:2]), rng.choice(present))
    if f.type is FieldType.STRING and rng.random() < 0.15:
        safe = [v for v in present if _pattern_safe(v)]
        if safe:
            v = rng.choice(safe)
            k = rng.randint(1, len(v))
            form = rng.choice(("prefix", "suffix", "infix", "exact"))
            pat = {"prefix": v[:k] + "%", "suffix": "%" + v[-k:], "infix": "%" + v[:k] + "%", "exact": v}[form]
            return leaf("LIKE", pat)
    op = rng.choice(_ORDERED_OPS)
    distinct = list({repr(v): v for v in present}.values())
    cross = f.type in NUMERIC_TYPES and rng.random() < cross_type_probability
    if op == "IN":
        k = rng.randint(1, min(3, len(distinct)))
        pivots = rng.sample(distinct, k)
        if cross:
            pivots = [_cross_type(rng, f.type, v) if rng.random() < 0.6 else v for v in pivots]
        elif f.type is FieldType.DECIMAL:
            pivots = [_decimal_pivot(rng, v) for v in pivots]
        return leaf(op, pivots)
    if op == "BETWEEN":
        a, b = sorted(rng.sample(distinct, 2) if len(distinct) >= 2 else [distinct[0], distinct[0]], key=_sort_key)
        if rng.random() < 0.3:
            b = _offset(f.type, b, -1)
        if cross:
            a, b = sorted([_cross_type(rng, f.type, a) if rng.random() < 0.6 else a, _cross_type(rng, f.type, b) if rng.random() < 0.6 else b], key=_sort_key)
        return leaf(op, [a, b])
    pivot = rng.choice(distinct)
    roll = rng.random()
    if roll < 0.3:
        pivot = _offset(f.type, pivot, rng.choice((-1, 1)))
    elif roll < 0.4:
        top = max(distinct, key=_sort_key)
        bottom = min(distinct, key=_sort_key)
        pivot = _offset(f.type, top, 1) if rng.random() < 0.5 else _offset(f.type, bottom, -1)
    if cross:
        pivot = _cross_type(rng, f.type, pivot)
    elif f.type is FieldType.DECIMAL:
        pivot = _decimal_pivot(rng, pivot)
    return leaf(op, pivot)


def _draw_map(rng: random.Random, on: str, f: FieldSpec, present: list, bind: str, leaf):
    """A predicate over an EMBEDDEDMAP LONG: `CONTAINSKEY :k` (a stored or absent
    key), `m.values() CONTAINS :p`, `m CONTAINSVALUE :p` (O7's face on a scan), or a
    dotted entry comparison `m.k op :p` under the usual operators and null tests --
    never `IS DEFINED`, which holds for every record through a dotted map key."""
    keys = sorted({k for m in present for k in m})
    elems = [x for m in present for x in m.values() if x is not None]
    roll = rng.random()
    if roll < 0.2 or not keys:
        return leaf("CONTAINSKEY", rng.choice(keys) if keys and rng.random() < 0.8 else rng.choice(MAP_KEYS))
    if roll < 0.32 and elems:
        # an absent value one past a stored one, kept a long (`2^63` is refused by
        # the HTTP layer's JSON parser: cov400 seeds 5030 / 5108)
        return leaf("VALUES CONTAINS", rng.choice(elems) if rng.random() < 0.8 else _offset(FieldType.INT, rng.choice(elems), 1))
    if roll < 0.4 and elems:
        return leaf("CONTAINSVALUE", rng.choice(elems))
    k = rng.choice(keys)
    kvals = [m[k] for m in present if m.get(k) is not None]
    spec = FieldSpec(f.name, FieldType.INT)
    inner = draw(rng, on, spec, kvals, bind) if kvals else None
    if inner is None or inner.op in ("IS DEFINED", "IS NOT DEFINED"):
        return Predicate(on, f.name, rng.choice(("IS NULL", "IS NOT NULL")), "", None, True, "int", False, k)
    return Predicate(on, f.name, inner.op, inner.bind, inner.pivot, True, "int", False, k)


def draw_insub(rng: random.Random, schema, instance, table: str, bind: str) -> "Predicate | None":
    """A semi-join leaf on `table`: `f IN (SELECT g FROM t2 [WHERE q])`, `g` a column
    of `f`'s type on any node class (the same one included), `q` drawn with even odds
    over `t2`'s fields under `<bind>s`. None when no column has a partner."""
    fields = [f for f in schema.query_fields(table) if f.type not in (FieldType.LIST, FieldType.MAP)]
    if not fields:
        return None
    f = rng.choice(fields)
    partners = [(t2.name, g) for t2 in schema.query_tables() for g in schema.query_fields(t2.name) if g.type is f.type]
    if not partners:
        return None
    t2, g = rng.choice(partners)
    q = None
    if rng.random() < 0.5:

        def inner_values(x):
            return [n.values.get(x.name) for n in instance.nodes_of(t2) if x.name in n.values]

        q = draw_over(rng, "source", schema.query_fields(t2), values_of=inner_values, bind=f"{bind}s")
    reference = q is None or q.reference
    return Predicate("source", f.name, "INSUB", "", sub_values(instance, t2, g.name, q), reference, f.type.value, f.ci, sub=(t2, g.name, q, False))


def draw_over(rng: random.Random, on: str, fields, values_of, bind: str, compound_probability: float = COMPOUND_PROBABILITY, depth: int = 0):
    """A predicate over a record with `fields` gives the
    column's stored values): a leaf on one field, or with `compound_probability` a
    compound of two leaves (`AND` / `OR`, possibly on ONE field -- a range or a
    disjunction of equalities) or a negation, at most two levels deep. Leaves inside
    a compound bind `<bind>_<k>`."""
    fields = list(fields)
    if not fields:
        return None
    if rng.random() >= compound_probability or depth >= 2:
        f = rng.choice(fields)
        return draw(rng, on, f, values_of(f), bind)
    op = rng.choice(COMPOUND_OPS)
    if op == "NOT":
        child = draw_over(rng, on, fields, values_of, f"{bind}_0", compound_probability * 0.5, depth + 1)
        return None if child is None else Compound(on, "NOT", (child,))
    children = []
    for k in range(2):
        child = draw_over(rng, on, fields, values_of, f"{bind}_{k}", compound_probability * 0.5, depth + 1)
        if child is None:
            return None
        children.append(child)
    return Compound(on, op, tuple(children))


def _sort_key(v):
    from ganda.orientdb.semantics import orient_key

    return orient_key(v)


# --- equivalent rewrites ----------------------------------------------------------------------


def rewrite(p, rng: random.Random, bind: str):
    """An equivalent concrete syntax of `p` under OrientDB's semantics (the null rules,
    two-valued NOT), or None when it has none. The rewritten leaves bind fresh names
    under `bind`, with the same values. This is the query-rewrite oracle: the pair
    (p, rewrite(p)) is the same question through a different planner path."""
    if p is None:
        return None
    if isinstance(p, Compound):
        if p.op == "NOT":
            c = p.children[0]
            if isinstance(c, Compound) and c.op in ("AND", "OR"):
                # De Morgan: NOT (a AND b) == (NOT a OR NOT b)
                return Compound(p.on, "OR" if c.op == "AND" else "AND", tuple(_negate(x) for x in c.children))
            return None
        # a AND b == NOT (NOT a OR NOT b)
        return Compound(p.on, "NOT", (Compound(p.on, "OR" if p.op == "AND" else "AND", tuple(_negate(c) for c in p.children)),))
    f, op, pv = p.field, p.op, p.pivot
    on = p.on

    def leaf(o, b, v):
        return Predicate(on, f, o, b, v, p.reference, p.ftype, p.ci, p.key, p.key_bind)

    if p.sub is not None:
        # the inner subquery over `(SELECT FROM t2)`: the same values, never
        # index-served (the outer lookup unchanged)
        t2, g, q, scan = p.sub
        return None if scan else dataclasses.replace(p, sub=(t2, g, q, True))
    if p.key is not None and not p.key_bind and rng.random() < 0.5:
        # the parameter-keyed bracket form of the same entry: `m[:kb] op :p`
        return Predicate(on, f, op, bind if op not in NO_BIND_OPS else "", pv, p.reference, p.ftype, p.ci, p.key, f"{bind}k")
    if op in MAP_OPS or not p.plain:
        return None
    if op in ("CONTAINSANY", "CONTAINSALL"):
        # `CONTAINSANY [a, b]` is `CONTAINS a OR CONTAINS b`, `CONTAINSALL` the AND
        if len(pv) == 1:
            return leaf("CONTAINS", bind, pv[0])
        return Compound(on, "OR" if op == "CONTAINSANY" else "AND", tuple(leaf("CONTAINS", f"{bind}_{k}", v) for k, v in enumerate(pv)))

    if op == "=":
        if p.ci and isinstance(pv, str) and pv.isascii() and rng.random() < 0.5:
            # a ci equality as a function over the lower-cased value: never index-served
            return leaf("LOWER=", bind, pv.lower())
        if p.ci and isinstance(pv, str):
            # `IN` ignores the collation on a scan (O21): not an equivalent concrete syntax
            return Compound(on, "NOT", (leaf("<>", bind, pv),))
        return leaf("IN", bind, [pv])
    if op == "<>":
        if p.ci and isinstance(pv, str) and pv.isascii():
            return Compound(on, "NOT", (leaf("LOWER=", bind, pv.lower()),))
        return Compound(on, "NOT", (leaf("=", bind, pv),))
    if op == "IN":
        if p.ci:
            return None  # `IN` is case-sensitive on a scan, `=` is not: no equivalent
        if len(pv) == 1:
            return leaf("=", bind, pv[0])
        return Compound(on, "OR", tuple(leaf("=", f"{bind}_{k}", v) for k, v in enumerate(pv)))
    if op == "BETWEEN":
        if p.ci or (p.ftype == "decimal" and any(lossy_pivot(FieldType.DECIMAL, b) for b in pv)):
            # a ci BETWEEN is case-sensitive where `>=` is not; a DECIMAL BETWEEN
            # converts a non-dyadic bound through its decimal string where `>=`
            # compares the binary expansion (O20): neither has an equivalent
            return None
        return Compound(on, "AND", (leaf(">=", f"{bind}_0", pv[0]), leaf("<=", f"{bind}_1", pv[1])))
    if op in (">", "<", ">=", "<="):
        # `NOT (f <= p)` also holds for a null f: the negation needs the null excluded
        neg = {">": "<=", "<": ">=", ">=": "<", "<=": ">"}[op]
        return Compound(on, "AND", (Compound(on, "NOT", (leaf(neg, bind, pv),)), leaf("IS NOT NULL", "", None)))
    if op == "IS NULL":
        return Compound(on, "NOT", (leaf("IS NOT NULL", "", None),))
    if op == "IS NOT NULL":
        return Compound(on, "NOT", (leaf("IS NULL", "", None),))
    if op == "IS DEFINED":
        return Compound(on, "NOT", (leaf("IS NOT DEFINED", "", None),))
    if op == "IS NOT DEFINED":
        return Compound(on, "NOT", (leaf("IS DEFINED", "", None),))
    return None


def _negate(p):
    return Compound(p.on, "NOT", (p,))
