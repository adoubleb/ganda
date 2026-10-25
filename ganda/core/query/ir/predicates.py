from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, NamedTuple

from ganda.core.query.ir.enums import CompoundOp, MapOp, NullOp, On

NO_BIND_OPS = tuple(NullOp)
MAP_OPS = tuple(MapOp)
COMPOUND_OPS = tuple(CompoundOp)


class Arith(NamedTuple):
    """Arithmetic on the field before the comparison: `(field <sym> :bind) op :constant`."""
    sym: str  # `+`, `-`, `*`
    bind: str  # the bind-variable name carrying the constant
    value: Any


class SubQuery(NamedTuple):
    table: str
    field: str
    pred: "Predicate | None"
    scan: bool


@dataclass(frozen=True)
class Predicate:
    on: On
    field: str
    op: str
    bind: str  # the bind-variable name carrying the constant ("" for a null op)
    constant: Any
    ftype: "str | None" = None  # the column's `FieldType.value`; None for an untyped embedded value
    case_insensitive: bool = False  # the column has a case-insensitive collation
    key: "str | None" = None  # a map entry: the leaf compares `field.key` (or `field[:kb]` when `key_bind`)
    key_bind: str = "" 
    rev: bool = False # reverse operand positions
    arith: "Arith | None" = None
    sub: "SubQuery | None" = None  # a semi-join (`INSUB`): the constant is the answered `field` list, nulls included
    literal: bool = False  # the constant written into the text instead of bound

    def __post_init__(self):
        object.__setattr__(self, "on", On(self.on))
        if self.arith is not None and not isinstance(self.arith, Arith):
            object.__setattr__(self, "arith", Arith(*self.arith))
        if self.sub is not None and not isinstance(self.sub, SubQuery):
            object.__setattr__(self, "sub", SubQuery(*self.sub))

    def to_dict(self) -> dict:
        d = {"on": self.on, "field": self.field, "op": self.op, "bind": self.bind, "constant": self.constant, "ftype": self.ftype, "case_insensitive": self.case_insensitive, "key": self.key, "key_bind": self.key_bind}
        if self.rev:
            d["rev"] = True
        if self.arith is not None:
            d["arith"] = list(self.arith)
        if self.literal:
            d["literal"] = True
        if self.sub is not None:
            s = self.sub
            d["sub"] = {"table": s.table, "field": s.field, "pred": s.pred.to_dict() if s.pred is not None else None, "scan": s.scan}
        return d

    @staticmethod
    def from_dict(d: "dict | None"):
        if d is None:
            return None
        if d.get("kind") == "compound":
            return Compound(d["on"], d["op"], tuple(Predicate.from_dict(c) for c in d["children"]))
        arith = Arith(*d["arith"]) if d.get("arith") else None
        sub = None
        if d.get("sub"):
            x = d["sub"]
            sub = SubQuery(x["table"], x["field"], Predicate.from_dict(x["pred"]), x["scan"])
        constant = d["constant"] if "constant" in d else d["pivot"]  # "pivot": a bundle written before 2026-10-25
        return Predicate(d["on"], d["field"], d["op"], d["bind"], constant, d.get("ftype"), d.get("case_insensitive", d.get("ci", False)), d.get("key"), d.get("key_bind", ""), d.get("rev", False), arith, sub, d.get("literal", False))

    @property
    def bind_vars(self) -> dict:
        if self.sub is not None:
            return dict(self.sub.pred.bind_vars) if self.sub.pred is not None else {}
        out = {self.key_bind: self.key} if self.key_bind else {}
        if self.arith is not None:
            out[self.arith.bind] = self.arith.value
        if self.op in NO_BIND_OPS or self.literal:
            return out
        if self.op == "BETWEEN":
            out.update({self.bind + "a": self.constant[0], self.bind + "b": self.constant[1]})
        else:
            out[self.bind] = self.constant
        return out

    def leaves(self) -> list:
        return [self]

    @property
    def signature(self) -> str:
        if self.sub is not None:
            return f"INSUB({self.sub.pred.signature if self.sub.pred is not None else '-'}{'@scan' if self.sub.scan else ''})"
        return f"{self.op}@key" if self.key is not None else self.op

    @property
    def plain(self) -> bool:
        """Neither reversed nor arithmetic: the form an index can serve."""
        return not self.rev and self.arith is None


@dataclass(frozen=True)
class Compound:
    on: On
    op: CompoundOp
    children: tuple

    def __post_init__(self):
        object.__setattr__(self, "on", On(self.on))
        object.__setattr__(self, "op", CompoundOp(self.op))

    def to_dict(self) -> dict:
        return {"kind": "compound", "on": self.on, "op": self.op, "children": [c.to_dict() for c in self.children]}

    @property
    def bind_vars(self) -> dict:
        out: dict = {}
        for c in self.children:
            out.update(c.bind_vars)
        return out

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
    return has_op(p, ops) or any(leaf.sub.pred is not None and has_op_deep(leaf.sub.pred, ops) for leaf in subqueries(p))


def sub_values(instance, table: str, field_name: str, q) -> list:
    """The `g` values `SELECT g FROM t2 [WHERE q]` answers: one per record, a null
    for an absent or null `g`."""
    rows = instance.nodes_of(table)
    sem = instance.schema.engine.semantics
    return [n.values.get(field_name) for n in rows if q is None or evaluate(n.values, q, sem)]


def resolve_subs(p, instance):
    """`p` with every semi-join's constant recomputed over `instance` (a tree drawn over
    one Instance evaluated over another: the transaction door's)."""
    if p is None:
        return None
    if isinstance(p, Compound):
        return Compound(p.on, p.op, tuple(resolve_subs(c, instance) for c in p.children))
    if p.sub is None:
        return p
    s = p.sub
    return dataclasses.replace(p, constant=sub_values(instance, s.table, s.field, resolve_subs(s.pred, instance)))


def evaluate(values: dict, p, semantics) -> bool:
    """Whether the record `values` satisfies the tree, under the engine's semantics
    (its leaf truth, `predicate_hit`, and its boolean logic)."""
    return semantics.evaluate(values, p)


# --- reversed operands --------------------------------------------------------

MIRROR = {"=": "=", "<>": "<>", "<": ">", ">": "<", "<=": ">=", ">=": "<="}
_REVERSIBLE = tuple(MIRROR) + ("HAS",)


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
