"""OrientDB SQL text for the abstract predicate IR (`predicates`): the engine's
parameter syntax (`:p1`), its operator concrete syntaxes (`m.values() CONTAINS`,
`f.toLowerCase() =`, `m[:k]`), the semi-join's subquery and the reversed and
arithmetic forms. Identical on every data model, the field expression being the data model's
own (`f1`, `in.f1`, `$n.f1`) via `prefix`."""
from __future__ import annotations

from ganda.orientdb.facade.predicates import MIRROR, NO_BIND_OPS, Compound
from ganda.orientdb.storage.dialect import sql_op
from ganda.orientdb.rules import assumes
from ganda.orientdb.rules.catalog import SQL


# every pivot is bound, and a bound double compares exactly (a literal one is O12)
assumes("exact.double.parameter", SQL, True)


def render_predicate(p, prefix: str = "") -> str:
    """The OrientDB text of a predicate tree; a compound is always parenthesized so
    the data models never rely on operator precedence."""
    if isinstance(p, Compound):
        if p.op == "NOT":
            return f"NOT ({render_predicate(p.children[0], prefix)})"
        return "(" + f" {p.op} ".join(render_predicate(c, prefix) for c in p.children) + ")"
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
            return f":{p.bind} IN {expr}"
        return f":{p.bind} {MIRROR[p.op]} {expr}"
    if p.op in NO_BIND_OPS:
        return f"{expr} {sql_op(p.op)}"
    if p.op == "BETWEEN":
        return f"{expr} BETWEEN :{p.bind}a AND :{p.bind}b"
    if p.op == "LOWER=":
        return f"{expr}.toLowerCase() = :{p.bind}"
    if p.op == "VALUES HAS":
        return f"{expr}.values() CONTAINS :{p.bind}"
    return f"{expr} {sql_op(p.op)} :{p.bind}"
