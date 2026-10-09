"""A predicate tree (`ganda.core.ast.predicates`) as SQL text for the column tables and as
a SPARQL filter for the triple store -- the same three-valued question on both
(`semantics`).

SQL: parameters are `:name` (the session binds them positionally; a list parameter
becomes `(?, ?, ...)` after `IN`, and `:name__json` is the canonical JSON text of the
list bound as `name`). A LIST column compares its text for `=` and asks its child table
for membership through `EXISTS`, which is two-valued as the semantics says.

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

from ganda.core.ast.predicates import MIRROR, Compound
from ganda.hana.dialect import child_table, sparql_literal

UNKNOWN_VAR = "?ganda_unknown"


# --- SQL --------------------------------------------------------------------------------------


class Names:
    """The SQL relation a logical table and a LIST column's elements are read from: the
    column tables themselves on `rel`; the doc data model's views over its collections
    (`concrete_syntax.document`) have names of their own."""

    prefix = ""

    def table(self, t: str) -> str:
        return self.prefix + t

    def child(self, t: str, f: str) -> str:
        return self.prefix + child_table(t, f)


REL = Names()


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
    if p.op in ("CONTAINS", "CONTAINSANY", "CONTAINSALL"):
        child = names.child(table, p.field)
        member = f"x.owner = {owner}"
        if p.op == "CONTAINS":
            cond = f":{p.bind} = x.v" if p.rev else f"x.v = :{p.bind}"
            return f"EXISTS (SELECT 1 FROM {child} x WHERE {member} AND {cond})"
        if p.op == "CONTAINSANY":
            return f"EXISTS (SELECT 1 FROM {child} x WHERE {member} AND x.v IN :{p.bind})"
        return f"(SELECT COUNT(DISTINCT x.v) FROM {child} x WHERE {member} AND x.v IN :{p.bind}) = {len(set(p.pivot))}"
    expr = col
    if p.arith is not None:
        expr = f"({col} {p.arith[0]} :{p.arith[1]})"
    if p.op in ("IS NULL", "IS NOT NULL"):
        return f"{expr} {p.op}"
    value = f":{p.bind}__json" if p.ftype == "list" and p.op in MIRROR else f":{p.bind}"
    if p.rev:
        return f"{value} {MIRROR[p.op]} {expr}"
    if p.op == "BETWEEN":
        return f"{expr} BETWEEN :{p.bind}a AND :{p.bind}b"
    if p.op in ("IN", "NOT IN"):
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
        if leaf.op in ("CONTAINS", "CONTAINSANY", "CONTAINSALL"):
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


def _leaf(p, record: str, regex: bool, native: bool) -> "tuple[str, str | None]":
    """(a leaf's test, the variable whose unboundness makes it UNKNOWN, or None for a
    two-valued test). The test itself never raises once that variable is bound."""
    v = var(record, p.field)
    if p.sub is not None:
        t2, g, q, _scan = p.sub
        inner = f"?gu a c:{t2} ; p:{g} ?gu_{g} ." + (f" {optionals(q, 'gu')} FILTER ({filter_expr(q, 'gu', regex, native)})" if q is not None else "") + f" FILTER (?gu_{g} = {v})"
        return f"EXISTS {{ {inner} }}", v
    if p.op in ("CONTAINS", "CONTAINSANY", "CONTAINSALL"):
        items = f"?{record} i:{p.field}"
        if p.op == "CONTAINS":
            if p.rev:
                return f"EXISTS {{ {items} ?gx . FILTER ({sparql_literal(p.pivot)} = ?gx) }}", None
            return f"EXISTS {{ {items} {sparql_literal(p.pivot)} }}", None
        if p.op == "CONTAINSANY":
            return f"EXISTS {{ {items} ?gx . FILTER (?gx IN ({', '.join(sparql_literal(x) for x in p.pivot)})) }}", None
        return "(" + " && ".join(f"EXISTS {{ {items} {sparql_literal(x)} }}" for x in p.pivot) + ")", None
    if p.op == "IS NULL":
        return f"!BOUND({v})", None
    if p.op == "IS NOT NULL":
        return f"BOUND({v})", None
    expr = v
    if p.arith is not None:
        # the constant in the column's own datatype: a double scaled by a double
        c = float(p.arith[2]) if p.ftype == "float" else p.arith[2]
        expr = f"({v} {p.arith[0]} {sparql_literal(c)})"
    if p.op == "LIKE":
        return _like(expr, p.pivot, regex), v
    if p.op == "BETWEEN":
        return f"({expr} >= {sparql_literal(p.pivot[0])} && {expr} <= {sparql_literal(p.pivot[1])})", v
    if p.op in ("IN", "NOT IN"):
        listed = ", ".join(sparql_literal(x) for x in p.pivot)
        return f"({expr} {p.op} ({listed}))", v
    op = "!=" if p.op == "<>" else p.op
    lit = sparql_literal(p.pivot)
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
