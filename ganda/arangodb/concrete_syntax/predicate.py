"""AQL text for the abstract predicate tree (`ganda.core.query.ir.predicates`): the engine's
parameter syntax (`@p1`), its operator forms (`IN_RANGE` for a range, `HAS()` for
definedness, `@p IN f` for a list membership, `ANY IN` / `ALL IN` for the quantified
ones, `HAS()` for a map's keys and `VALUES()` for its values), the semi-join's subquery and the reversed and
arithmetic forms. Identical on every data model: the record is the document variable
`var` (`n`, `v1`, `ed1`, `CURRENT`), and a field is `var.f`.
"""
from __future__ import annotations

import re

from ganda.core.query.ir.predicates import MIRROR, NO_BIND_OPS, Compound
from ganda.arangodb.rules import answer, assumes, works_around
from ganda.arangodb.rules.catalog import ACCEPTED, AQL, REFUSED

_OPS = {"=": "==", "<>": "!=", "<": "<", "<=": "<=", ">": ">", ">=": ">="}
# every variable of a query is fresh (a semi-join's are named after the leaf's bind)
assumes("accepts.variable.redeclared", AQL, REFUSED)
# `VALUES()` rejects a non-object under failOnWarning (errorNum 1542): the value test
# answers false for one through a ternary, which evaluates the taken branch only --
# and never through `n.m || {}`: a filter the optimizer moves into a traversal is
# evaluated on the start vertex too, where a number under `m` is the number
assumes("accepts.values.non_object_argument", AQL, REFUSED)
works_around("A41")
# `HAS()` answers false for a non-object, so the key test needs no ternary
_HAS_KEY = "HAS({expr}, @{bind})" if answer("accepts.has.non_object_argument", AQL) is ACCEPTED else "(IS_OBJECT({expr}) ? HAS({expr}, @{bind}) : false)"


def render_predicate(p, var: str = "n", schema=None) -> str:
    """The AQL text of a predicate tree over document variable `var`; a compound is
    always parenthesized so no form relies on operator precedence. `schema` resolves
    a semi-join over the base class (the inheritance axis) into the union of the node
    collections."""
    if isinstance(p, Compound):
        if p.op == "NOT":
            return f"NOT ({render_predicate(p.children[0], var, schema)})"
        return "(" + f" {p.op} ".join(render_predicate(c, var, schema) for c in p.children) + ")"
    expr = f"{var}.{p.field}"
    if p.sub is not None:
        t2, g, q, _scan = p.sub
        # every variable of a query must be fresh: the subquery's are named after the
        # leaf's bind (two semi-joins in one filter bind two names)
        tag = re.sub(r"[^A-Za-z0-9_]", "_", p.bind or (q.leaves()[0].bind if q is not None else "sub"))
        s, m = f"s_{tag}", f"m_{tag}"
        if schema is not None and schema.is_base(t2):
            from ganda.arangodb.concrete_syntax.support import base_union

            src = base_union(schema, m)
        else:
            src = t2
        inner = f"FOR {s} IN {src}" + (f" FILTER {render_predicate(q, s, schema)}" if q is not None else "") + f" RETURN {s}.{g}"
        return f"{expr} IN ({inner})"
    if p.op == "IS PRESENT":
        return f"HAS({var}, '{p.field}')"
    if p.op == "IS ABSENT":
        return f"NOT HAS({var}, '{p.field}')"
    if p.key is not None:
        expr = f"{expr}[@{p.key_bind}]" if p.key_bind else f"{expr}.{p.key}"
    if p.arith is not None:
        expr = f"({expr} {p.arith.sym} @{p.arith.bind})"
    if p.rev:
        if p.op == "HAS":
            return f"{expr} ANY == @{p.bind}"
        return f"@{p.bind} {_OPS[MIRROR[p.op]]} {expr}"
    if p.op in NO_BIND_OPS:
        return f"{expr} == null" if p.op == "IS NULL" else f"{expr} != null"
    if p.op == "BETWEEN":
        return f"IN_RANGE({expr}, @{p.bind}a, @{p.bind}b, true, true)"
    if p.op == "LIKE":
        return f"{expr} LIKE @{p.bind}"
    if p.op == "IN":
        return f"{expr} IN @{p.bind}"
    if p.op == "NOT IN":
        return f"{expr} NOT IN @{p.bind}"
    if p.op == "HAS":
        return f"@{p.bind} IN {expr}"
    if p.op == "HAS ANY":
        return f"{expr} ANY IN @{p.bind}"
    if p.op == "HAS ALL":
        return f"@{p.bind} ALL IN {expr}"
    if p.op == "HAS KEY":
        return _HAS_KEY.format(expr=expr, bind=p.bind)
    if p.op in ("VALUES HAS", "HAS VALUE"):
        return f"(IS_OBJECT({expr}) ? @{p.bind} IN VALUES({expr}) : false)"
    return f"{expr} {_OPS[p.op]} @{p.bind}"
