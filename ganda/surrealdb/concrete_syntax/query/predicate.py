"""SurrealQL text for the abstract predicate IR (`predicates`): the engine's bind
variables (`$p1`), its operator spellings (`CONTAINS`, `object::keys(m) CONTAINS`,
`m[$k]`), the semi-join's subquery and the reversed, arithmetic and literal forms.
Identical on every data model, the field expression being the data model's own (`f1`,
`out.f1`, `$l.f1`) via `prefix`.

The engine's null rules the text keeps the core's semantics under: an omitted field is
NONE and an explicit null NULL, both below every value (`<`, `<=`, `!=` hold, `=`,
`>`, `>=`, `IN` fail), so `IS NULL` is `IS NONE OR IS NULL`, `IS PRESENT` is `IS NOT
NONE`; a function or arithmetic over a missing value is REFUSED, so a method, a cast
or an arithmetic leaf is guarded by the field's presence (and answers false for a
missing value: `predicate_steering.evaluate` mirrors that). A datetime, decimal or
byte-string constant is always written as a typed literal (a `LET` carries no type).
"""
from __future__ import annotations

import json

from ganda.core.data.model import DATE_TYPES, FieldType
from ganda.core.query.ir.predicates import MIRROR, Compound
from ganda.surrealdb.engine.dialect import surql_literal
from ganda.surrealdb.rules import assumes
from ganda.surrealdb.rules.catalog import SURQL

# the types whose constants travel as literals in the text (a bind variable is untyped)
LITERAL_TYPES = frozenset(t.value for t in DATE_TYPES) | {FieldType.DECIMAL.value, FieldType.BINARY.value}
# the comparisons a case-insensitive column lowercases both sides of
CI_OPS = ("=", "<>", "<", "<=", ">", ">=", "IN")

OPERATORS = {"<>": "!=", "HAS": "CONTAINS", "HAS ANY": "CONTAINSANY", "HAS ALL": "CONTAINSALL"}

assumes("compare.none_and_null.below_every_value", SURQL, True)
assumes("compare.none.equals_null", SURQL, False)
assumes("value.absent_is_null", SURQL, False)


def surql_op(op: str) -> str:
    return OPERATORS.get(op, op)


def _ftype(p) -> "FieldType | None":
    try:
        return FieldType(p.ftype) if p.ftype else None
    except ValueError:
        return None


def _literal(v, p) -> str:
    return surql_literal(v, _ftype(p))


def _constant(p, part: "str | None" = None) -> str:
    """The leaf's constant: the bind variable, or the literal (drawn, or forced for a
    typed column)."""
    v = p.constant[0 if part == "a" else 1] if part else p.constant
    if p.literal or p.ftype in LITERAL_TYPES:
        return _literal(v, p)
    return f"${p.bind}{part or ''}"


def arith_bound(expr: str, sym: str, c) -> str:
    """The range of an integer field whose `expr <sym> c` stays inside the 64-bit range
    (the engine refuses an overflow, which would fail the statement on every data model
    alike); the empty text when nothing can overflow."""
    from ganda.core.constants import I64_MAX, I64_MIN

    if sym == "+":
        return f"{expr} <= {I64_MAX - c}" if c > 0 else (f"{expr} >= {I64_MIN - c}" if c < 0 else "")
    if sym == "-":
        return f"{expr} >= {I64_MIN + c}" if c > 0 else (f"{expr} <= {I64_MAX + c}" if c < 0 else "")
    if sym == "*" and abs(c) > 1:
        return f"math::abs({expr}) <= {I64_MAX // abs(c)}"
    return ""


def present(expr: str) -> str:
    return f"{expr} IS NOT NONE AND {expr} IS NOT NULL"


def missing(expr: str) -> str:
    return f"{expr} IS NONE OR {expr} IS NULL"


def tables_of(schema, table: str) -> str:
    """The FROM list of a class: the subclass tables of the base class (the engine has
    no inheritance), else the table."""
    if schema is not None and schema.is_base(table):
        return ", ".join(nt.name for nt in schema.node_types)
    return table


def _like(expr: str, p) -> str:
    """`LIKE` as the string function its pattern shape names: a prefix, a suffix, an
    infix or an exact match (the generator builds a pattern from a value holding no
    wildcard). The function refuses a missing value, so it is guarded."""
    pat = p.constant
    lead, trail = pat.startswith("%"), pat.endswith("%") and len(pat) > 1
    body = json.dumps(pat.strip("%"), ensure_ascii=False)
    if p.case_insensitive:
        subject, body = f"string::lowercase({expr})", json.dumps(pat.strip("%").lower(), ensure_ascii=False)
    else:
        subject = expr
    if not lead and not trail:
        return f"({present(expr)} AND {subject} = {body})"
    fn = "string::contains" if lead and trail else "string::ends_with" if lead else "string::starts_with"
    return f"({present(expr)} AND {fn}({subject}, {body}))"


def _ci(expr: str, op: str, constant: str) -> str:
    """A case-insensitive comparison: both sides lowercased (a missing value keeps
    the null rules: `!=` holds, the others fail)."""
    rhs = f"array::map({constant}, |$x| string::lowercase($x))" if op == "IN" else f"string::lowercase({constant})"
    if op == "<>":
        return f"({missing(expr)} OR string::lowercase({expr}) != {rhs})"
    return f"({present(expr)} AND string::lowercase({expr}) {surql_op(op)} {rhs})"


def render_predicate(p, prefix: str = "", element: bool = False, schema=None) -> str:
    """The SurrealQL text of a predicate tree; a compound is always parenthesized so
    the data models never rely on operator precedence. `element`: the prefix is a
    link entry (`$l.`), whose fields are read the same way."""
    if isinstance(p, Compound):
        if p.op == "NOT":
            return f"NOT ({render_predicate(p.children[0], prefix, element, schema)})"
        return "(" + f" {p.op} ".join(render_predicate(c, prefix, element, schema) for c in p.children) + ")"
    expr = prefix + p.field
    if p.sub is not None:
        t2, g, q, _scan = p.sub
        where = [f"({render_predicate(q, schema=schema)})"] if q is not None else []
        where.append(present(g))
        return f"{expr} IN (SELECT VALUE {g} FROM {tables_of(schema, t2)} WHERE {' AND '.join(where)})"
    if p.op == "HAS KEY":
        return f"object::keys({expr} ?? {{}}) CONTAINS {_constant(p)}"
    if p.op in ("VALUES HAS", "HAS VALUE"):
        return f"object::values({expr} ?? {{}}) CONTAINS {_constant(p)}"
    if p.key is not None:
        expr = f"{expr}[${p.key_bind}]" if p.key_bind else f"{expr}.{p.key}"
    if p.op == "IS NULL":
        return f"({missing(expr)})"
    if p.op == "IS NOT NULL":
        return f"({present(expr)})"
    if p.op == "IS PRESENT":
        return f"{expr} IS NOT NONE"
    if p.op == "IS ABSENT":
        return f"{expr} IS NONE"
    if p.op == "LIKE":
        return _like(expr, p)
    if p.arith is not None:
        c = p.arith.value
        lit = surql_literal(c, FieldType.DECIMAL) if p.ftype == "decimal" else (f"${p.arith.bind}" if not p.literal else surql_literal(c))
        guard = present(expr)
        bound = arith_bound(expr, p.arith.sym, c) if p.ftype in ("int", "int32") else None
        if bound:
            guard = f"{guard} AND {bound}"
        expr = f"({expr} {p.arith.sym} {lit})"
        return f"({guard} AND {_compare(expr, p)})"
    if p.case_insensitive and p.op in CI_OPS and p.ftype == "string":
        return _ci(expr, p.op, _constant(p))
    return _compare(expr, p)


def _compare(expr: str, p) -> str:
    if p.rev:
        if p.op == "HAS":
            return f"{_constant(p)} IN {expr}"
        return f"{_constant(p)} {surql_op(MIRROR[p.op])} {expr}"
    if p.op == "BETWEEN":
        return f"({expr} >= {_constant(p, 'a')} AND {expr} <= {_constant(p, 'b')})"
    return f"{expr} {surql_op(p.op)} {_constant(p)}"
