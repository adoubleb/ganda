"""The predicate tree shared by every query AST, its sampler and its evaluation.

A predicate is a tree: a `Predicate` LEAF is `(op, pivot)` over one field; a `Compound`
joins children with `AND` / `OR` or negates one with `NOT`. The pivot of a leaf sits ON
a stored value, ADJACENT to one, or OUTSIDE the range (bugs live at boundaries). The
operator names are the AST's own vocabulary; an engine's concrete syntax renders them
and its `Semantics.predicate_hit` says what each one answers.

Two adversarial axes a leaf can carry:

- a CROSS-TYPE numeric pivot (`cross_type_probability`): a pivot the column does not
  hold exactly (a double on an integer column, ...), drawn by the engine's
  `pivots.cross_type`, where an index lookup and a scan may convert differently;
- a NULL pivot never arises from drawing (the null rules are asked through the null
  tests), but a value join binds a record's own field, which may be null.

A leaf's `bind` is a bare parameter NAME (`p1`); the engine's parameter syntax is the
renderer's. A SEMI-JOIN leaf (`INSUB`, the select AST only) asks whether `f` is among
the `g` values of `t2`'s records matching `q`: its pivot is the list of `g` values the
subquery answers over the Instance it was drawn over (`resolve_subs` recomputes it over
another), and it binds only `q`'s pivots. A `LIKE` pattern is built from a stored value
that holds none of the engine's wildcard characters.

Functions that need the engine take it explicitly (`evaluate(values, p, semantics)`,
`draw(..., engine)`) or find it on the schema (`instance.schema.engine`).
"""
from __future__ import annotations

import dataclasses
import math
import random
import struct
from dataclasses import dataclass
from typing import Any

from ganda.core.model import MAP_KEYS, NUMERIC_TYPES, FieldSpec, FieldType

I64_MAX = 2**63 - 1
I64_MIN = -(2**63)
I32_MAX = 2**31 - 1
I32_MIN = -(2**31)

_EQUALITY_OPS = ("=", "<>", "IN")
# the null tests bind nothing (`IS [NOT] DEFINED` tells an absent field from a null)
NO_BIND_OPS = ("IS NULL", "IS NOT NULL", "IS DEFINED", "IS NOT DEFINED")
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
    # the way it serves the plain one, so a `<data model>:rev` variant is the
    # index-vs-scan oracle inside one database.
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
    sem = instance.schema.engine.semantics
    return [n.values.get(field_name) for n in rows if q is None or evaluate(n.values, q, sem)]


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


def evaluate(values: dict, p, semantics) -> bool:
    """Whether the record `values` satisfies the tree, under the engine's semantics
    (its leaf truth, `predicate_hit`, and its boolean logic)."""
    return semantics.evaluate(values, p)


# --- reversed operands --------------------------------------------------------

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


# --- pivots: on a stored value, adjacent to one, outside the range ------------


def _f32(v: float) -> float:
    try:
        return struct.unpack("f", struct.pack("f", v))[0]
    except OverflowError:
        return v


def shift_date(v: str, seconds: int) -> str:
    import datetime as _dt

    try:
        t = _dt.datetime.strptime(v, "%Y-%m-%d %H:%M:%S") + _dt.timedelta(seconds=seconds)
    except (ValueError, OverflowError):
        return v
    if t.year < 1 or t.year > 9999:
        return v
    return t.strftime("%Y-%m-%d %H:%M:%S")


def offset(ftype: FieldType, v, direction: int):
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
        return shift_date(v, direction)
    if ftype is FieldType.DATE:
        return shift_date(v, 86400 * direction)
    if ftype is FieldType.STRING:
        # the code-unit successor; the predecessor of a non-empty string is its
        # longest proper prefix (a strictly smaller string in every order)
        return v + chr(0) if direction > 0 else (v[:-1] if v else v)
    return v


def draw(rng: random.Random, on: str, f: FieldSpec, values: list, bind: str, engine, cross_type_probability: float = CROSS_TYPE_PROBABILITY) -> "Predicate | None":
    """A LEAF predicate on `f` whose stored (non-null) values are `values`. None when
    the column holds nothing to aim at."""
    present = [v for v in values if v is not None]
    ft, ci = f.type.value, f.ci
    pivots_of, _sort_key = engine.pivots, engine.semantics.key

    def leaf(op, pivot):
        return Predicate(on, f.name, op, "" if op in NO_BIND_OPS else bind, pivot, True, ft, ci)

    roll = rng.random()
    if roll < 0.12:
        return leaf(rng.choice(pivots_of.NULL_OPS), None)
    if not present:
        return None
    if f.type is FieldType.LIST:
        # CONTAINS carries most of the weight; a whole-list equality and CONTAINSANY /
        # CONTAINSALL over one or two elements take the rest
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
                # a pivot just past a stored element -- inside the 64-bit range, or
                # the value could not travel as an integer at all
                pivots[-1] = pivots[-1] + 1 if pivots[-1] < I64_MAX else pivots[-1] - 1
            return leaf(rng.choice(("CONTAINSANY", "CONTAINSALL")), pivots)
        return leaf("CONTAINS", rng.choice(elems))
    if f.type is FieldType.MAP:
        return _draw_map(rng, on, f, present, bind, leaf, engine)
    if f.type is FieldType.BOOL:
        return leaf(rng.choice(_EQUALITY_OPS[:2]), rng.choice(present))
    if f.type is FieldType.STRING and rng.random() < 0.15:
        safe = [v for v in present if v != "" and not any(c in v for c in pivots_of.LIKE_WILDCARDS)]
        if safe:
            v = rng.choice(safe)
            k = rng.randint(1, len(v))
            form = rng.choice(("prefix", "suffix", "infix", "exact"))
            pat = {"prefix": v[:k] + "%", "suffix": "%" + v[-k:], "infix": "%" + v[:k] + "%", "exact": v}[form]
            return leaf("LIKE", pat)
    op = rng.choice(pivots_of.ORDERED_OPS)
    distinct = list({repr(v): v for v in present}.values())
    cross = f.type in NUMERIC_TYPES and rng.random() < cross_type_probability
    if op == "IN":
        k = rng.randint(1, min(3, len(distinct)))
        pivots = rng.sample(distinct, k)
        if cross:
            pivots = [pivots_of.cross_type(rng, f.type, v) if rng.random() < 0.6 else v for v in pivots]
        else:
            pivots = [pivots_of.refine(rng, f.type, v) for v in pivots]
        return leaf(op, pivots)
    if op == "BETWEEN":
        a, b = sorted(rng.sample(distinct, 2) if len(distinct) >= 2 else [distinct[0], distinct[0]], key=_sort_key)
        if rng.random() < 0.3:
            b = offset(f.type, b, -1)
        if cross:
            a, b = sorted([pivots_of.cross_type(rng, f.type, a) if rng.random() < 0.6 else a, pivots_of.cross_type(rng, f.type, b) if rng.random() < 0.6 else b], key=_sort_key)
        return leaf(op, [a, b])
    pivot = rng.choice(distinct)
    roll = rng.random()
    if roll < 0.3:
        pivot = offset(f.type, pivot, rng.choice((-1, 1)))
    elif roll < 0.4:
        top = max(distinct, key=_sort_key)
        bottom = min(distinct, key=_sort_key)
        pivot = offset(f.type, top, 1) if rng.random() < 0.5 else offset(f.type, bottom, -1)
    if cross:
        pivot = pivots_of.cross_type(rng, f.type, pivot)
    else:
        pivot = pivots_of.refine(rng, f.type, pivot)
    return leaf(op, pivot)


def _draw_map(rng: random.Random, on: str, f: FieldSpec, present: list, bind: str, leaf, engine):
    """A predicate over a map of integers: `CONTAINSKEY` (a stored or absent key),
    `VALUES CONTAINS` / `CONTAINSVALUE` (a stored or absent value), or an entry
    comparison `m.k op p` under the usual operators and null tests -- never a
    definedness test, which a map entry answers differently per engine."""
    keys = sorted({k for m in present for k in m})
    elems = [x for m in present for x in m.values() if x is not None]
    roll = rng.random()
    if roll < 0.2 or not keys:
        return leaf("CONTAINSKEY", rng.choice(keys) if keys and rng.random() < 0.8 else rng.choice(MAP_KEYS))
    if roll < 0.32 and elems:
        # an absent value one past a stored one, kept inside the 64-bit range
        return leaf("VALUES CONTAINS", rng.choice(elems) if rng.random() < 0.8 else offset(FieldType.INT, rng.choice(elems), 1))
    if roll < 0.4 and elems:
        return leaf("CONTAINSVALUE", rng.choice(elems))
    k = rng.choice(keys)
    kvals = [m[k] for m in present if m.get(k) is not None]
    spec = FieldSpec(f.name, FieldType.INT)
    inner = draw(rng, on, spec, kvals, bind, engine) if kvals else None
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

        q = draw_over(rng, "source", schema.query_fields(t2), values_of=inner_values, bind=f"{bind}s", engine=schema.engine)
    reference = q is None or q.reference
    return Predicate("source", f.name, "INSUB", "", sub_values(instance, t2, g.name, q), reference, f.type.value, f.ci, sub=(t2, g.name, q, False))


def draw_over(rng: random.Random, on: str, fields, values_of, bind: str, engine, compound_probability: float = COMPOUND_PROBABILITY, depth: int = 0):
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
        return draw(rng, on, f, values_of(f), bind, engine)
    op = rng.choice(COMPOUND_OPS)
    if op == "NOT":
        child = draw_over(rng, on, fields, values_of, f"{bind}_0", engine, compound_probability * 0.5, depth + 1)
        return None if child is None else Compound(on, "NOT", (child,))
    children = []
    for k in range(2):
        child = draw_over(rng, on, fields, values_of, f"{bind}_{k}", engine, compound_probability * 0.5, depth + 1)
        if child is None:
            return None
        children.append(child)
    return Compound(on, op, tuple(children))
