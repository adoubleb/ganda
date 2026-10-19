"""The graph data model's openCypher: the Property Graph Engine asked through SQL's
`OPENCYPHER_TABLE` over the database's workspace (`dialect`: one workspace `gw` over one
view per table, the views' columns the workspace's ATTRIBUTES -- `ID`, `EID`, `SRC_ID`,
`DST_ID`, a base field as itself, an own field `<TABLE>_<FIELD>`).

Two families of concrete syntax, both reaching the engine:

- the VIEWS family (`NAMES`: `g_<table>`): every rel SQL renderer runs unchanged over
  CTEs that read each table THROUGH the graph engine --

      g_n1 AS (SELECT * FROM OPENCYPHER_TABLE(GRAPH WORKSPACE gw QUERY
          'MATCH (a:n1) RETURN a.ID AS ID, a.N1_F1 AS F1, ...'))
      g_e1 AS (SELECT * FROM OPENCYPHER_TABLE(... 'MATCH (a:n1)-[e:e1]->(b:n2)
          RETURN e.EID AS EID, a.ID AS SRC_ID, b.ID AS DST_ID, e.E1_W AS W'))

  so a join, a membership, a method, a median and a semi-join are SQL over what
  openCypher projected (`with_views` prepends the CTEs a text names; a LIST column's
  child table stays the physical one, the engine holding no list);
- the NATIVE family: openCypher states the question -- the pattern, the filter
  (`cypher_filter`), the aggregate, the ordering -- and SQL only wraps what the
  language lacks (a start with no path, COUNT DISTINCT, NOT EXISTS, MIN over levels).

The language is a subset (RULES.md, G*): ONE MATCH clause (a heterogeneous workspace,
which every campaign's is, takes no second one: probed G8) of comma-separated patterns
and one RETURN; no incoming arrow (a hop in is the pattern reversed), no OPTIONAL
MATCH, UNION, WITH or IN; comparisons, IS NULL, CONTAINS / STARTS WITH / ENDS WITH over
a CONSTANT, arithmetic, AND / OR / NOT; COUNT / MAX / MIN / SUM over numbers, grouped
implicitly by the other RETURN items -- SUM over no present value may answer 0 (the
docs) or NULL (probed), so every native sum carries a COUNT beside it and the SQL
outside turns the empty case into NULL. A relationship binds once within the clause, so
a multi-hop native text is asked only where no edge could repeat (pairwise distinct edge
types; the reference's walk semantics would differ otherwise). Expressions take INT /
BIGINT / DOUBLE / NVARCHAR attributes; a BOOLEAN is projected only. ORDER BY puts NULL
last ascending (HANA SQL: first), so a native ordering over a nullable field is not
asked.

Parameters and constants: a DOUBLE pivot is bound, a cypher `$name` in `PARAMETERS
('name' = :name)` outside the literal (`Params`; `connection.positional` skips the
quoted query), under the SQL bind name `predicate.sql` uses, so a case's `bind_vars`
serve both; every other pivot -- an integer, a string (`\'` and `\\` the escapes), a
LIST's text -- is a constant in the query (`literal`). So one OPENCYPHER_TABLE call
binds parameters of one type: a statement holding two calls types the second call's
bare parameters in the wrong order when they mix a string and a number (A9, G32); a
double constant would lose digits beyond the fifteenth (A8, G31: a bound double and
`TOFLOAT` of a string constant compare exactly), while an integer and a string constant
are exact (G30, G34).

The result columns are typed by the workspace (unlike SPARQL_TABLE), so a projection
needs no decode suffix except a LIST's `__JSON`; aliases are uppercase so a SQL wrapper
reads them unquoted and the session lowercases them.

The module constants named after probe facts (`WALK_ACROSS_CLAUSES`, ...) hold the
documented behaviour until the fact is probed (RULES.md); each selects a rendering.
"""
from __future__ import annotations

import re

from ganda.core.ir.predicates import MIRROR, Compound
from ganda.core.model import FieldType, Schema
from ganda.hana.concrete_syntax.predicate import Names
from ganda.hana.dialect import SQL_TYPES, child_table, graph_attr, graph_columns, graph_label, list_text, opencypher_table, param_expr

# --- probe-selected switches (the fact in RULES.md; the default is the documented answer) -----

# G8 (probed 2026-10-11): a heterogeneous workspace -- every campaign's -- takes ONE
# MATCH clause ("For heterogeneous graphs only one match clause is allowed"), so a
# relationship binds once across the hops: no walk semantics across clauses
WALK_ACROSS_CLAUSES = False
# G10 (probed 2026-10-11): a BOOLEAN attribute in WHERE is refused (projected only)
BOOL_FILTERS = False
# G5 (probed 2026-10-11): ORDER BY ASC puts NULL LAST, unlike HANA SQL's default
NULLS_LIKE_SQL = False
# G6: strings order by code unit, as HANA SQL does
STRING_ORDER_NATIVE = True
# G4: an aggregate-only RETURN over no match answers one row (COUNT 0, MAX NULL)
AGG_EMPTY_ROW = True
# G12: OPENCYPHER_TABLE sees the transaction's own uncommitted writes
TX_VISIBLE = True
# G1: a correlated scalar subquery over a `g_` CTE answers (no H2-like crash)
SCALAR_OK = True
# G19: an OPENCYPHER_TABLE nested inside IN (...) / NOT IN (...) answers
NESTED_TABLE_FN_OK = True
# G9: variable-length paths `[*k..k]` run (the path `range` and the reach forms)
VARLEN_OK = True
# G2 (probed 2026-10-11): parameters are bound bare (`:p`, typed by the engine); a CAST
# in the PARAMETERS clause is refused ("parameter initialization with values")
TYPED_PARAMS = False


# --- the views family ----------------------------------------------------------------------


class _GraphNames(Names):
    prefix = "g_"
    # a LIST column's elements: the physical child table (the graph holds no list)
    count_membership = True

    def child(self, t: str, f: str) -> str:
        return child_table(t, f)


NAMES = _GraphNames()


def label(table: str) -> str:
    return graph_label(table)


def attr(schema: Schema, table: str, field: str) -> str:
    """A field's attribute name in the workspace (`dialect.graph_attr`): a base-class
    field keeps its name, an own field is qualified by its table."""
    shared = schema.base is not None and any(f.name == field for f in schema.base.fields)
    return graph_attr(table, field, shared)


def scan_items(schema: Schema, table: str, var: str) -> str:
    """`a.ID AS ID, a.N1_F1 AS F1, ...`: every column of a table read back under the
    physical column's name (uppercase), the RETURN list of a view's query."""
    return ", ".join(f"{var}.{a} AS {c.upper()}" for c, a in graph_columns(schema, table))


def _is_edge(schema: Schema, table: str) -> bool:
    return any(et.name == table for et in schema.edge_types)


def view_query(schema: Schema, table: str) -> str:
    """The openCypher text behind `g_<table>`: a class's records, or an edge type's
    relationships with the endpoints the engine resolved (`a.ID AS SRC_ID`)."""
    if not _is_edge(schema, table):
        return f"MATCH {vertex(schema, table, 'a')} RETURN {scan_items(schema, table, 'a')}"
    et = schema.edge_type(table)
    attrs = ", ".join(f"e.{a} AS {c.upper()}" for c, a in graph_columns(schema, table) if c not in ("eid", "src_id", "dst_id"))
    items = "e.EID AS EID, a.ID AS SRC_ID, b.ID AS DST_ID" + (f", {attrs}" if attrs else "")
    return f"MATCH (a:{label(et.from_type)})-[e:{label(table)}]->(b:{label(et.to_type)}) RETURN {items}"


def view_definition(schema: Schema, name: str) -> str:
    table = name[len(NAMES.prefix):]
    return f"{name} AS (SELECT * FROM {opencypher_table(view_query(schema, table))})"


# a view is `g_` and a generated class or edge type name (`n3`, `e12`), or the base
# class of the inheritance axis (`N0`: the unlabelled MATCH over every vertex table)
_VIEW_NAME = re.compile(r"\bg_[neN]\d+\b")


def with_views(schema: Schema, text: str) -> str:
    """`text` with the CTE of every `g_` view it names in front (as `document.with_views`)."""
    used = sorted(set(_VIEW_NAME.findall(text)))
    if not used:
        return text
    return f"WITH {', '.join(view_definition(schema, v) for v in used)} {text}"


# --- the native family: parameters, literals ----------------------------------------------------


class Unsupported(ValueError):
    """The question has no openCypher form (the views family asks it)."""


_SQL_TYPE_OF = {"int": SQL_TYPES[FieldType.INT], "float": SQL_TYPES[FieldType.FLOAT], "string": SQL_TYPES[FieldType.STRING], "list": SQL_TYPES[FieldType.LIST], "bool": SQL_TYPES[FieldType.BOOL]}


class Params:
    """The PARAMETERS clause of ONE `OPENCYPHER_TABLE` call: every `$name` the query
    uses, in first-use order, bound from the SQL bind of the same name."""

    def __init__(self):
        self.items: dict = {}

    def use(self, name: str, ftype: "str | None" = None) -> str:
        if name not in self.items:
            self.items[name] = param_expr(name, _SQL_TYPE_OF.get(ftype or "") if TYPED_PARAMS else None)
        return f"${name}"


def table_fn(cypher: str, params: "Params | None" = None) -> str:
    """The SQL table function of a query: `OPENCYPHER_TABLE(GRAPH WORKSPACE gw QUERY
    '...' PARAMETERS (...))`, declaring exactly the parameters the query uses (one
    `Params` serves every call of a rendering; a call declares no unused name)."""
    used = {k: v for k, v in params.items.items() if re.search(rf"\${re.escape(k)}\b", cypher)} if params is not None else None
    return opencypher_table(cypher, used)


def cypher_string(s: str) -> str:
    """A cypher string constant: `\\'` and `\\\\` are the escapes the grammar has; a
    control character has none (the question stays with the parameters)."""
    out = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == "'":
            out.append("\\'")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise Unsupported(f"no cypher string for {s!r}")
        else:
            out.append(ch)
    return "'" + "".join(out) + "'"


def literal(value, ftype: "str | None" = None) -> str:
    """A constant inlined in a cypher text: an integer as its digits (exact, G30), a
    string or a LIST's canonical text as a string constant (G34). A double has NO
    constant: the parser keeps about fifteen significant digits of one (A8, G31), so a
    double is always bound; a boolean has no constant either."""
    if isinstance(value, bool) or value is None or isinstance(value, float) or ftype == "float":
        raise Unsupported(f"no cypher constant for {value!r}")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return cypher_string(list_text(value))
    return cypher_string(str(value))


# --- the native family: a predicate tree as a cypher filter ------------------------------------


def _like(expr: str, pattern: str) -> str:
    """A LIKE pattern as the string predicate it is: a prefix `STARTS WITH`, a suffix
    `ENDS WITH`, an infix `CONTAINS`, else an equality. The body never holds a
    wildcard (the generator draws the pattern from a stored value without one)."""
    body = pattern.strip("%")
    if any(c in body for c in "%_"):
        raise Unsupported(f"no cypher form of LIKE {pattern!r}")
    if pattern.startswith("%") and pattern.endswith("%") and len(pattern) > 1:
        return f"{expr} CONTAINS {cypher_string(body)}"
    if pattern.endswith("%"):
        return f"{expr} STARTS WITH {cypher_string(body)}"
    if pattern.startswith("%"):
        return f"{expr} ENDS WITH {cypher_string(body)}"
    return f"({expr} = {cypher_string(pattern)})"


def _crosses_value(ftype: "str | None", v) -> bool:
    """Whether one pivot value is of the other numeric type than its column (a double
    against INT, an integer against FLOAT: `pivots.cross_type`)."""
    return (ftype == "int" and type(v) is float) or (ftype == "float" and type(v) is int)


def crosses(p) -> bool:
    """Whether any of a numeric leaf's pivot values is of the other numeric type."""
    if p.ftype not in ("int", "float") or p.op in ("IS NULL", "IS NOT NULL") or p.sub is not None:
        return False
    pv = p.pivot if isinstance(p.pivot, list) else [p.pivot]
    return any(_crosses_value(p.ftype, v) for v in pv)


def cypher_filter(p, var: str, table: str, schema: Schema, params: "Params | None") -> str:
    """The cypher text of a tree over the record variable `var` of class or edge type
    `table`. Three-valued logic is the engine's own (a comparison with NULL is NULL,
    dropped by WHERE; NOT / AND / OR combine as in SQL), so the text is the tree.

    A DOUBLE pivot is BOUND (`$name`, `params`); every other pivot -- an integer, a
    string, a LIST's text -- is a constant in the text, so one OPENCYPHER_TABLE call
    binds parameters of one type only: a statement with two calls types the second
    call's bare parameters in the wrong order when they mix a string and a number
    (A9, G32), a double constant loses digits (A8, G31), an integer constant and a
    string constant are exact (G30, G34). Raises `Unsupported` for a membership, a
    semi-join, a case-insensitive or (unless `BOOL_FILTERS`) a boolean comparison, and
    for a string constant the grammar cannot spell (a control character)."""
    if isinstance(p, Compound):
        if p.op == "NOT":
            return f"NOT ({cypher_filter(p.children[0], var, table, schema, params)})"
        return "(" + f" {p.op} ".join(cypher_filter(c, var, table, schema, params) for c in p.children) + ")"
    if p.sub is not None or p.ci or p.key is not None:
        raise Unsupported(f"no cypher form of {p.signature}")
    if p.op in ("CONTAINS", "CONTAINSANY", "CONTAINSALL"):
        raise Unsupported("a LIST membership has no cypher form")
    if p.ftype == "bool" and not BOOL_FILTERS:
        raise Unsupported("a BOOLEAN attribute is projected only")
    # a cross-type pivot (`pivots.cross_type`): the engine refuses a BIGINT attribute
    # against a float and a DOUBLE attribute against an integer outright (G36, `[4906]
    # Type mismatch`), so for THAT value both sides are doubles -- an integer attribute
    # through TOFLOAT, the pivot as TOFLOAT of its text (exact, unlike a double
    # constant: A8). Per value, as SQL types each comparison of an IN list on its own: an
    # integer pivot beside the cross-typed one keeps the exact integer comparison (a
    # BIGINT above 2^53 through TOFLOAT would meet its neighbours)
    cross = crosses(p)

    def value(name: str, v, ftype: "str | None"):
        if ftype == "float" or isinstance(v, float):
            if params is None:
                raise Unsupported("a double has no cypher constant")
            return params.use(name, "float")
        return literal(v, ftype)

    col = f"{var}.{attr(schema, table, p.field)}"
    if p.op in ("IS NULL", "IS NOT NULL"):
        if p.arith is not None:
            raise Unsupported("IS NULL takes an attribute, not an expression")
        return f"({col} IS NULL)" if p.op == "IS NULL" else f"NOT ({col} IS NULL)"
    expr = col
    if p.arith is not None and cross:
        raise Unsupported("arithmetic over a cross-type pivot")
    if p.arith is not None:
        sym, cbind, c = p.arith
        expr = f"({col} {sym} {value(cbind, float(c) if p.ftype == 'float' else c, p.ftype)})"

    def sides(name: str, v) -> "tuple[str, str]":
        """(the attribute's side, the pivot's side) for one value."""
        if _crosses_value(p.ftype, v):
            return (f"TOFLOAT({expr})" if p.ftype == "int" else expr), f"TOFLOAT('{v!r}')"
        return expr, value(name, v, p.ftype)

    if p.op == "LIKE":
        return _like(expr, p.pivot)
    if p.op == "BETWEEN":
        la, ra = sides(p.bind + "a", p.pivot[0])
        lb, rb = sides(p.bind + "b", p.pivot[1])
        return f"({la} >= {ra} AND {lb} <= {rb})"
    if p.op in ("IN", "NOT IN"):
        if not p.pivot:
            raise Unsupported("an empty IN list")
        ors = " OR ".join("{} = {}".format(*sides(f"{p.bind}__{k}", v)) for k, v in enumerate(p.pivot))
        return f"({ors})" if p.op == "IN" else f"NOT ({ors})"
    if p.op not in MIRROR:
        raise Unsupported(f"no cypher form of {p.op}")
    if p.ftype == "list":
        lhs, rhs = expr, literal(p.pivot, "list")
    else:
        lhs, rhs = sides(p.bind, p.pivot)
    if p.rev:
        return f"({rhs} {MIRROR[p.op]} {lhs})"
    return f"({lhs} {p.op} {rhs})"


def expressible(p, schema: Schema, table: str) -> bool:
    """Whether `cypher_filter` renders the tree (None: nothing to render)."""
    if p is None:
        return True
    try:
        cypher_filter(p, "t", table, schema, Params())
    except Unsupported:
        return False
    return True


# --- the native family: one table's filtered projection -----------------------------------------


def vertex(schema: Schema, table: str, var: str) -> str:
    """`(t:n1)` for a class; `(t)` for the base class of the inheritance axis: an
    unlabelled vertex spans every vertex table (G21), which is what every subclass's
    records are, and a base-class attribute keeps one name across them (`attr`)."""
    return f"({var})" if schema.is_base(table) else f"({var}:{label(table)})"


def match_clause(schema: Schema, table: str, var: str) -> str:
    """`MATCH (t:n1)` for a class (`MATCH (t)` for the base class); `MATCH
    (a:n1)-[t:e1]->(b:n2)` for an edge type (the relationship bound as `t`, its
    endpoints as `a` and `b`)."""
    if not _is_edge(schema, table):
        return f"MATCH {vertex(schema, table, var)}"
    et = schema.edge_type(table)
    return f"MATCH (a:{label(et.from_type)})-[{var}:{label(table)}]->(b:{label(et.to_type)})"


def scan(schema: Schema, table: str, pred) -> str:
    """One table filtered by openCypher and projected whole, as a table function:
    `OPENCYPHER_TABLE(... 'MATCH (t:n1) WHERE ... RETURN t.ID AS ID, t.N1_F1 AS F1, ...')`
    -- the FROM source of a SQL text that does the rest (`select`'s `match` position)."""
    params = Params()
    where = f" WHERE {cypher_filter(pred, 't', table, schema, params)}" if pred is not None else ""
    if _is_edge(schema, table):
        attrs = ", ".join(f"t.{a} AS {c.upper()}" for c, a in graph_columns(schema, table) if c not in ("eid", "src_id", "dst_id"))
        items = "t.EID AS EID, a.ID AS SRC_ID, b.ID AS DST_ID" + (f", {attrs}" if attrs else "")
    else:
        items = scan_items(schema, table, "t")
    return table_fn(f"{match_clause(schema, table, 't')}{where} RETURN {items}", params)
