"""A predicate tree (`ganda.core.query.ir.predicates`) as SQL text for the column tables and as
a SPARQL filter for the triple store -- the same three-valued question on both
(`semantics`).

SQL: parameters are `:name` (the session binds them positionally; a list parameter
becomes `(?, ?, ...)` after `IN`, and `:name__json` is the canonical JSON text of the
list bound as `name`). A LIST column compares its text for `=` and asks its child table
for membership through `EXISTS`, which is two-valued as the semantics says. A bare
parameter takes the COLUMN's type (a BIGINT column's `?` truncates 1.5 to 1), so a
cross-type constant -- a double against an INT column (`predicate_sampling.cross_type`) -- is written
`CAST(:name AS DOUBLE)`, one cast per element of an IN list (`:name__k`), and the column
converts to DOUBLE for the comparison (probe R21).

SPARQL: no parameters reach the engine, so the values are literals in the text
(`dialect.sparql_literal`). A field is a variable `?<record>_<field>` bound by an
`OPTIONAL` triple pattern (unbound for a null field); `IS NULL` is `!BOUND(...)`;
membership is an `EXISTS` over the `i:` triples; a semi-join an `EXISTS` over the
subquery's class. Two renderings of the logic (`filter_expr`): by default every leaf
that a null makes unknown is guarded (`BOUND(?f) && test` for TRUE, `BOUND(?f) &&
!test` for FALSE) and NOT / AND / OR combine the TRUE and FALSE forms, so the filter
never raises; the NATIVE rendering leaves the unknowns to SPARQL's own error
propagation (an unbound variable is an error; a semi-join over a null field
`IF(BOUND(?f), EXISTS {...}, ?ganda_unknown)`, a variable never bound) -- the same
question asked of the engine's handling of errors, a variant of its own. The variables an `EXISTS` binds start with `?g` (`?gx`,
`?gu`), a prefix no record variable of a query uses: an EXISTS pattern sees the outer
solution's bindings.
"""
from __future__ import annotations

from ganda.core.query.ir.predicates import MIRROR, Compound
from ganda.core.rules.vocabulary import Logic
from ganda.hana.rules import assumes, works_around
from ganda.hana.rules.catalog import SPARQL
from ganda.hana.storage.dialect import child_table, sparql_literal

# the rdf `native` variants rest on SPARQL's error propagation (three-valued as SQL);
# the never-bound variable is declared by a never-matching OPTIONAL (A6)
assumes("filter.logic", SPARQL, Logic.KLEENE)
works_around("A6")

UNKNOWN_VAR = "?ganda_unknown"


# --- SQL --------------------------------------------------------------------------------------


class Names:
    """The SQL relation a logical table and a LIST column's elements are read from: the
    column tables themselves on `rel`; the doc data model's views over its collections
    (`concrete_syntax.document`) have names of their own."""

    prefix = ""
    # CONTAINSALL as a count of the matching elements (a correlated scalar subquery);
    # off, as a conjunction of one EXISTS per element
    count_membership = True

    def table(self, t: str) -> str:
        return self.prefix + t

    def child(self, t: str, f: str) -> str:
        return self.prefix + child_table(t, f)


REL = Names()


def crosses(p) -> bool:
    """Whether a leaf's constant is of the other numeric type than its INT column (a double
    constant on an int column; an integer constant on a DOUBLE column needs no cast: the
    parameter takes the column's type exactly)."""
    if p.ftype != "int" or p.op in ("IS NULL", "IS NOT NULL") or p.sub is not None:
        return False
    pv = p.constant if isinstance(p.constant, list) else [p.constant]
    return any(isinstance(v, float) for v in pv)


def _param(name: str, value, cross: bool) -> str:
    return f"CAST(:{name} AS DOUBLE)" if cross and isinstance(value, float) else f":{name}"


def sql(p, alias: str = "", table: str = "", key: str = "id", names: Names = REL) -> str:
    """The SQL text of a tree over a record of `table` (its child tables are named after
    it) reached as `alias` (no alias: bare column names), keyed by column `key`."""
    if isinstance(p, Compound):
        if p.op == "NOT":
            return f"NOT ({sql(p.children[0], alias, table, key, names)})"
        return "(" + f" {p.op} ".join(sql(c, alias, table, key, names) for c in p.children) + ")"
    col = f"{alias}.{p.field}" if alias else p.field
    owner = f"{alias}.{key}" if alias else key
    if p.sub is not None:
        t2, g, q, scan = p.sub
        src = f"(SELECT * FROM {names.table(t2)}) u" if scan else f"{names.table(t2)} u"
        inner = f"u.{g} IS NOT NULL" + (f" AND {sql(q, 'u', t2, 'id', names)}" if q is not None else "")
        return f"{col} IN (SELECT u.{g} FROM {src} WHERE {inner})"
    if p.op in ("HAS", "HAS ANY", "HAS ALL"):
        child = names.child(table, p.field)
        member = f"x.owner = {owner}"
        if p.op == "HAS":
            cond = f":{p.bind} = x.v" if p.rev else f"x.v = :{p.bind}"
            return f"EXISTS (SELECT 1 FROM {child} x WHERE {member} AND {cond})"
        if p.op == "HAS ANY":
            return f"EXISTS (SELECT 1 FROM {child} x WHERE {member} AND x.v IN :{p.bind})"
        if not names.count_membership:
            if not p.constant:
                return "1 = 1"
            return "(" + " AND ".join(f"EXISTS (SELECT 1 FROM {child} x WHERE {member} AND x.v = :{p.bind}__{k})" for k in range(len(p.constant))) + ")"
        return f"(SELECT COUNT(DISTINCT x.v) FROM {child} x WHERE {member} AND x.v IN :{p.bind}) = {len(set(p.constant))}"
    expr = col
    if p.arith is not None:
        expr = f"({col} {p.arith.sym} :{p.arith.bind})"
    if p.op in ("IS NULL", "IS NOT NULL"):
        return f"{expr} {p.op}"
    cross = crosses(p)
    value = f":{p.bind}__json" if p.ftype == "list" and p.op in MIRROR else _param(p.bind, p.constant, cross)
    if p.rev:
        return f"{value} {MIRROR[p.op]} {expr}"
    if p.op == "BETWEEN":
        return f"{expr} BETWEEN {_param(p.bind + 'a', p.constant[0], cross)} AND {_param(p.bind + 'b', p.constant[1], cross)}"
    if p.op in ("IN", "NOT IN"):
        if cross:
            return f"{expr} {p.op} (" + ", ".join(_param(f"{p.bind}__{k}", v, True) for k, v in enumerate(p.constant)) + ")"
        return f"{expr} {p.op} :{p.bind}"
    return f"{expr} {p.op} {value}"


# --- SPARQL -----------------------------------------------------------------------------------


def var(record: str, field: str) -> str:
    return f"?{record}_{field}"


def fields_used(p, record: str) -> list:
    """(record, field) of every scalar field a tree's filter reads (membership and the
    semi-join's own class are asked inside their EXISTS)."""
    out: list = []
    for leaf in (p.leaves() if p is not None else []):
        if leaf.op in ("HAS", "HAS ANY", "HAS ALL"):
            continue
        if (record, leaf.field) not in out:
            out.append((record, leaf.field))
    return out


def optionals(p, record: str) -> str:
    """The OPTIONAL patterns binding the fields a tree reads of `?record`."""
    return " ".join(f"OPTIONAL {{ ?{r} p:{f} {var(r, f)} }}" for r, f in fields_used(p, record))


def _like(v: str, pattern: str, regex: bool) -> str:
    body = pattern.strip("%")
    if regex:
        import re

        rx = re.sub(r"([\\.^$|?*+()\[\]{}])", r"\\\1", body)
        rx = ("" if pattern.startswith("%") else "^") + rx + ("" if pattern.endswith("%") else "$")
        return f"REGEX({v}, {sparql_literal(rx)})"
    if pattern.startswith("%") and pattern.endswith("%") and len(pattern) > 1:
        return f"CONTAINS({v}, {sparql_literal(body)})"
    if pattern.endswith("%"):
        return f"STRSTARTS({v}, {sparql_literal(body)})"
    if pattern.startswith("%"):
        return f"STRENDS({v}, {sparql_literal(body)})"
    return f"({v} = {sparql_literal(pattern)})"


def _lit(p, value) -> str:
    """A constant as a SPARQL literal: its own type -- except an integer constant against a
    FLOAT field (`predicate_sampling.cross_type`), written as the double it equals: the triple store
    compares `IN` by numeric promotion either way (probe K24), the offline stand-in
    (rdflib) compares `IN` by term, so the same-typed literal keeps the two in step
    while the SQL, collection and cypher texts still ask the cross-type question."""
    if p.ftype == "float" and isinstance(value, int) and not isinstance(value, bool):
        return sparql_literal(float(value))
    return sparql_literal(value)


def _leaf(p, record: str, regex: bool, native: bool) -> "tuple[str, str | None]":
    """(a leaf's test, the variable whose unboundness makes it UNKNOWN, or None for a
    two-valued test). The test itself never raises once that variable is bound."""
    v = var(record, p.field)
    if p.sub is not None:
        t2, g, q, _scan = p.sub
        inner = f"?gu a c:{t2} ; p:{g} ?gu_{g} ." + (f" {optionals(q, 'gu')} FILTER ({filter_expr(q, 'gu', regex, native)})" if q is not None else "") + f" FILTER (?gu_{g} = {v})"
        return f"EXISTS {{ {inner} }}", v
    if p.op in ("HAS", "HAS ANY", "HAS ALL"):
        items = f"?{record} i:{p.field}"
        if p.op == "HAS":
            if p.rev:
                return f"EXISTS {{ {items} ?gx . FILTER ({sparql_literal(p.constant)} = ?gx) }}", None
            return f"EXISTS {{ {items} {sparql_literal(p.constant)} }}", None
        if p.op == "HAS ANY":
            return f"EXISTS {{ {items} ?gx . FILTER (?gx IN ({', '.join(sparql_literal(x) for x in p.constant)})) }}", None
        return "(" + " && ".join(f"EXISTS {{ {items} {sparql_literal(x)} }}" for x in p.constant) + ")", None
    if p.op == "IS NULL":
        return f"!BOUND({v})", None
    if p.op == "IS NOT NULL":
        return f"BOUND({v})", None
    expr = v
    if p.arith is not None:
        # the constant in the column's own datatype: a double scaled by a double
        c = float(p.arith.value) if p.ftype == "float" else p.arith.value
        expr = f"({v} {p.arith.sym} {sparql_literal(c)})"
    if p.op == "LIKE":
        return _like(expr, p.constant, regex), v
    if p.op == "BETWEEN":
        return f"({expr} >= {_lit(p, p.constant[0])} && {expr} <= {_lit(p, p.constant[1])})", v
    if p.op in ("IN", "NOT IN"):
        listed = ", ".join(_lit(p, x) for x in p.constant)
        return f"({expr} {p.op} ({listed}))", v
    op = "!=" if p.op == "<>" else p.op
    lit = _lit(p, p.constant)
    if p.rev:
        mirrored = "!=" if MIRROR[p.op] == "<>" else MIRROR[p.op]
        return f"({lit} {mirrored} {expr})", v
    return f"({expr} {op} {lit})", v


def _truth(p, record: str, regex: bool, want: bool) -> str:
    """An expression that holds exactly where the tree is TRUE (`want`) or FALSE (not
    `want`), and never raises: three-valued logic spelled out with BOUND guards."""
    if isinstance(p, Compound):
        if p.op == "NOT":
            return _truth(p.children[0], record, regex, not want)
        conj = (p.op == "AND") == want
        return "(" + (" && " if conj else " || ").join(_truth(c, record, regex, want) for c in p.children) + ")"
    test, guard = _leaf(p, record, regex, False)
    if guard is None:
        return test if want else f"!({test})"
    return f"(BOUND({guard}) && {test})" if want else f"(BOUND({guard}) && !({test}))"


def filter_expr(p, record: str, regex: bool = False, native: bool = False) -> str:
    """The SPARQL expression of a tree over `?record` (its fields bound by `optionals`):
    by default three-valued logic spelled out (`_truth`), so the answer rests on no
    engine's handling of errors; `native` leaves the unknowns to SPARQL's own error
    propagation through `!`, `&&` and `||` (an unbound variable is an error; a
    semi-join over a null field an `IF` whose else branch is an unbound variable)."""
    if not native:
        return _truth(p, record, regex, True)
    if isinstance(p, Compound):
        if p.op == "NOT":
            return f"!({filter_expr(p.children[0], record, regex, True)})"
        joiner = " && " if p.op == "AND" else " || "
        return "(" + joiner.join(filter_expr(c, record, regex, True) for c in p.children) + ")"
    test, guard = _leaf(p, record, regex, True)
    if p.sub is not None:
        return f"IF(BOUND({guard}), {test}, {UNKNOWN_VAR})"
    return test


def sparql_filter(p, record: str, regex: bool = False, native: bool = False) -> str:
    """`OPTIONAL {...} ... FILTER (...)` for a tree over `?record`, or ''."""
    if p is None:
        return ""
    opts = optionals(p, record)
    expr = filter_expr(p, record, regex, native)
    if UNKNOWN_VAR in expr:
        # HANA rejects a FILTER that names a variable bound nowhere in the pattern (an
        # internal error, not a per-solution evaluation error): the never-bound unknown
        # of a native semi-join is DECLARED by a never-matching OPTIONAL so it parses,
        # yet stays unbound so SPARQL's error propagation keeps the record out (probe K6)
        opts = (opts + " " if opts else "") + f"OPTIONAL {{ ?{record} <urn:ganda:never> {UNKNOWN_VAR} }}"
    return (opts + " " if opts else "") + f"FILTER ({expr})"
