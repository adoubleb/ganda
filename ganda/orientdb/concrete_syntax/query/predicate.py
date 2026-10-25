"""OrientDB SQL text for the abstract predicate IR (`predicates`): the engine's
parameter syntax (`:p1`), its operator concrete syntaxes (`m.values() CONTAINS`,
`m[:k]`), the semi-join's subquery and the reversed, arithmetic and literal
forms. Identical on every data model, the field expression being the data model's
own (`f1`, `in.f1`, `$n.f1`) via `prefix`."""
from __future__ import annotations

from ganda.orientdb.facade.predicates import MIRROR, NO_BIND_OPS, Compound
from ganda.orientdb.rules import assumes
from ganda.orientdb.rules.catalog import SQL

# The core operators OrientDB SQL spells differently; the rest are written as named.
OPERATORS = {
    "HAS": "CONTAINS",
    "HAS ANY": "CONTAINSANY",
    "HAS ALL": "CONTAINSALL",
    "HAS KEY": "CONTAINSKEY",
    "HAS VALUE": "CONTAINSVALUE",
    "IS PRESENT": "IS DEFINED",
    "IS ABSENT": "IS NOT DEFINED",
}


def sql_op(op: str) -> str:
    """A core predicate operator as OrientDB SQL writes it."""
    return OPERATORS.get(op, op)


# a constant is bound unless the leaf writes it into the text, and a bound double compares
# exactly


def _literal(v) -> str:
    """A NUMERIC constant as SQL text -- the one kind of value that is safe to interpolate
    (a string never is). A float keeps its full repr."""
    if isinstance(v, list):
        return "[" + ", ".join(_literal(x) for x in v) + "]"
    if isinstance(v, bool):
        return "true" if v else "false"
    return repr(v)


def _constant(p, suffix: str = "") -> str:
    if not p.literal:
        return f":{p.bind}{suffix}"
    return _literal(p.constant[0 if suffix == "a" else 1] if suffix else p.constant)


# `IS DEFINED` on a list element's field holds for an absent one (an O8 face): the
# element's keys tell
assumes("element.keys.defined_fields", SQL, True)


def render_predicate(p, prefix: str = "", element: bool = False) -> str:
    """The OrientDB text of a predicate tree; a compound is always parenthesized so
    the data models never rely on operator precedence. With `element` the prefix is a
    list element (`e[k].`), whose definedness tests read its keys."""
    if isinstance(p, Compound):
        if p.op == "NOT":
            return f"NOT ({render_predicate(p.children[0], prefix, element)})"
        return "(" + f" {p.op} ".join(render_predicate(c, prefix, element) for c in p.children) + ")"
    if element and p.op in ("IS PRESENT", "IS ABSENT") and p.key is None:
        has = f"{prefix.removesuffix('.')}.keys() CONTAINS '{p.field}'"
        return has if p.op == "IS PRESENT" else f"NOT ({has})"
    expr = prefix + p.field
    if p.sub is not None:
        t2, g, q, _scan = p.sub
        return f"{expr} IN (SELECT {g} FROM {t2}" + (f" WHERE {render_predicate(q)}" if q is not None else "") + ")"
    if p.key is not None:
        expr = f"{expr}[:{p.key_bind}]" if p.key_bind else f"{expr}.{p.key}"
    if p.arith is not None:
        expr = f"({expr} {p.arith.sym} :{p.arith.bind})"
    if p.rev:
        if p.op == "HAS":
            return f"{_constant(p)} IN {expr}"
        return f"{_constant(p)} {MIRROR[p.op]} {expr}"
    if p.op in NO_BIND_OPS:
        return f"{expr} {sql_op(p.op)}"
    if p.op == "BETWEEN":
        return f"{expr} BETWEEN {_constant(p, 'a')} AND {_constant(p, 'b')}"
    if p.op == "VALUES HAS":
        return f"{expr}.values() CONTAINS {_constant(p)}"
    return f"{expr} {sql_op(p.op)} {_constant(p)}"
