"""AQL text for the abstract predicate tree (`ganda.core.ir.predicates`): the engine's
parameter syntax (`@p1`), its operator forms (`IN_RANGE` for a range, `HAS()` for
definedness, `@p IN f` for a list membership, `ANY IN` / `ALL IN` for the quantified
ones, `VALUES()` for a map's values), the semi-join's subquery and the reversed and
arithmetic forms. Identical on every data model: the record is the document variable
`var` (`n`, `v1`, `ed1`, `CURRENT`), and a field is `var.f`.

With `noopt` every field expression is wrapped in `NOOPT()`, which the optimizer may
not look through: the filter is then never index-served (the scan twin of the plain
form on a database holding an index).
"""
from __future__ import annotations

import re

from ganda.core.ir.predicates import MIRROR, NO_BIND_OPS, Compound

_OPS = {"=": "==", "<>": "!=", "<": "<", "<=": "<=", ">": ">", ">=": ">="}


def render_predicate(p, var: str = "n", noopt: bool = False, schema=None) -> str:
    """The AQL text of a predicate tree over document variable `var`; a compound is
    always parenthesized so no form relies on operator precedence. `schema` resolves
    a semi-join over the base class (the inheritance axis) into the union of the node
    collections."""
    if isinstance(p, Compound):
        if p.op == "NOT":
            return f"NOT ({render_predicate(p.children[0], var, noopt, schema)})"
        return "(" + f" {p.op} ".join(render_predicate(c, var, noopt, schema) for c in p.children) + ")"
    expr = f"{var}.{p.field}"
    if p.sub is not None:
        t2, g, q, scan = p.sub
        # every variable of a query must be fresh: the subquery's are named after the
        # leaf's bind (two semi-joins in one filter bind two names)
        tag = re.sub(r"[^A-Za-z0-9_]", "_", p.bind or (q.leaves()[0].bind if q is not None else "sub"))
        s, m = f"s_{tag}", f"m_{tag}"
        if schema is not None and schema.is_base(t2):
            src = "UNION(" + ", ".join(f"(FOR {m}_{nt.name} IN {nt.name} RETURN {m}_{nt.name})" for nt in schema.node_types) + ")"
        else:
            src = f"(FOR {m} IN {t2} RETURN {m})" if scan else t2
        inner = f"FOR {s} IN {src}" + (f" FILTER {render_predicate(q, s, False, schema)}" if q is not None else "") + f" RETURN {s}.{g}"
        return f"{_wrap(expr, noopt)} IN ({inner})"
    if p.op == "IS DEFINED":
        return f"HAS({var}, '{p.field}')"
    if p.op == "IS NOT DEFINED":
        return f"NOT HAS({var}, '{p.field}')"
    if p.key is not None:
        expr = f"{expr}[@{p.key_bind}]" if p.key_bind else f"{expr}.{p.key}"
    expr = _wrap(expr, noopt)
    if p.arith is not None:
        expr = f"({expr} {p.arith.sym} @{p.arith.bind})"
    if p.rev:
        if p.op == "CONTAINS":
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
    if p.op == "CONTAINS":
        return f"@{p.bind} IN {expr}"
    if p.op == "CONTAINSANY":
        return f"{expr} ANY IN @{p.bind}"
    if p.op == "CONTAINSALL":
        return f"@{p.bind} ALL IN {expr}"
    if p.op == "CONTAINSKEY":
        # `HAS` and `VALUES` reject a non-object (errorNum 1542 under failOnWarning), so
        # a null or absent map answers false through a ternary -- NOT `m || {}`: a filter
        # the optimizer moves into a traversal loses the default (A41)
        return f"(IS_OBJECT({expr}) ? HAS({expr}, @{p.bind}) : false)"
    if p.op in ("VALUES CONTAINS", "CONTAINSVALUE"):
        return f"(IS_OBJECT({expr}) ? @{p.bind} IN VALUES({expr}) : false)"
    return f"{expr} {_OPS[p.op]} @{p.bind}"


def _wrap(expr: str, noopt: bool) -> str:
    return f"NOOPT({expr})" if noopt else expr
