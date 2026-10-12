"""The doc data model's SQL: the JSON Document Store asked directly (a COLLECTION query,
its filter evaluated by the document store) and read relationally (VIEWS over the
collections, joined in SQL).

A collection holds NVARCHAR documents (`CREATE COLLECTION`, the default return type):
a projected field reaches SQL as text, so a number is cast (`TO_BIGINT`, `TO_DOUBLE`), a
boolean read as its text and turned into a BOOLEAN outside the collection query, a LIST
array read as its JSON text (decoded by the session: a column alias ending in `__json`).
A parameter is never compared bare (an ambiguous parameter type is refused, by design):
it is `CAST(? AS <type>)`, a number with a fraction compared as the DECIMAL the store
keeps it as (`:p__dec`, the session binds the double's shortest decimal), a boolean
`TO_JSON_BOOLEAN(TRUE)`, a list `PARSE_JSON(:p__json)`.

The VIEWS (`NAMES`: `v_<table>`) give the doc data model the column tables' shape, so
every rel syntax runs over them unchanged:

    v_n1(id, f1, ...)          the collection's documents, every field typed
    v_n1__f3(owner, v)         a LIST field's elements (`UNNEST "f3" AS "x"`)
    v_e1(eid, src_id, dst_id, w, ...)   the link objects embedded in e1's source
                               class (`UNNEST "e1" AS "l"`), `src_id` the document's id
    v_e1__f(owner, v)          a LIST attribute's elements (two chained UNNESTs)

`with_views` prepends the definitions of the views a text names. A whole-list `=`
compares the list's text, which the view cannot promise is canonical: a question with
one is asked of the collection itself, never of the views (`compares_list_text`).

The base class of the inheritance axis (`N0`) is no collection: `v_N0` is the UNION ALL
of every subclass's view (its id and the base-class fields), `v_N0__b1` of their element
views, defined after the views they read; a base-class question has the views as its
only doc engine path (`select.collection_asks` says no).

The collection filter's NOT: `doc_pred` pushes a negation down to the leaves by default
(the documented grammar shows no NOT); with `keep_not` it writes `NOT (...)` as drawn,
and `NOT (FOR ANY ...)` for a negated membership (D8, D19, D20: the store evaluates both
three-valued, as SQL) -- the `doc:neg` variant.
"""
from __future__ import annotations

import re

from ganda.core.ast.predicates import MIRROR, Compound
from ganda.core.model import FieldType, Schema
from ganda.hana.concrete_syntax.predicate import Names



class _DocNames(Names):
    prefix = "v_"
    # the CONTAINSALL count is a correlated scalar subquery over an UNNEST element view,
    # which crashes the Document Store (H2); one EXISTS per element asks the same
    count_membership = False


NAMES = _DocNames()


def path(field: str, alias: str = "") -> str:
    """A field of the document, with the document root specifier (`$."f"`: optional on a
    collection returning NVARCHAR, recommended, and it keeps a field from resolving as
    a schema or collection name), or of the unnested element `alias` (`"l"."f"`: an
    alias takes no root specifier)."""
    return f'"{alias}"."{field}"' if alias else f'$."{field}"'


# --- typed reads ------------------------------------------------------------------------------


def typed(ftype: FieldType, expr: str) -> str:
    """A field read as its SQL type inside a collection query (a boolean stays text: it
    is turned into a BOOLEAN outside, `boolean_of`)."""
    if ftype is FieldType.INT:
        return f"TO_BIGINT({expr})"
    if ftype is FieldType.FLOAT:
        return f"TO_DOUBLE({expr})"
    return expr


def boolean_of(col: str) -> str:
    return f"CASE {col} WHEN 'true' THEN TRUE WHEN 'false' THEN FALSE END"


def projection(ftype: FieldType, expr: str, alias: str) -> str:
    """`<typed field> AS <alias>` for a projected value, the alias carrying the decoding
    the session applies to text (`__bool`, `__json`)."""
    if ftype is FieldType.BOOL:
        return f"{expr} AS {alias}__bool"
    if ftype is FieldType.LIST:
        return f"{expr} AS {alias}__json"
    return f"{typed(ftype, expr)} AS {alias}"


# --- the views --------------------------------------------------------------------------------


def sql_cast(ftype: FieldType, col: str) -> str:
    """A projected field (the collection's text) as its SQL type, in SQL: a number by
    TO_BIGINT / TO_DOUBLE of its text, a boolean by its text, a string by TO_NVARCHAR.
    A STRING is cast explicitly because a value that reads numeric (`"0"`) is otherwise
    inferred as a number by the Document Store, and a string comparison against it fails
    (`Conversion ... to BIGINT ... 'A'`)."""
    if ftype is FieldType.INT:
        return f"TO_BIGINT({col}) AS {col}"
    if ftype is FieldType.FLOAT:
        return f"TO_DOUBLE({col}) AS {col}"
    if ftype is FieldType.BOOL:
        return f"{boolean_of(col)} AS {col}"
    if ftype is FieldType.STRING:
        return f"TO_NVARCHAR({col}) AS {col}"
    return col


def view_definition(schema: Schema, name: str) -> str:
    """The SQL of one view (`v_n1`, `v_n1__f3`, `v_e1`, `v_e1__f`).

    A LIST-element view (`__field`) reads the collection and UNNESTs it directly. A main
    view (`v_n1`, `v_e1`) is TWO CTEs, `raw_<view>` and `<view>`: the raw CTE reads the
    document fields as text straight from the collection, the view casts them over the
    raw CTE. The split is required because the Document Store's SQL dialect rejects a
    BOOLEAN constant (`CASE ... THEN TRUE`, the boolean cast) in any query that reads a
    collection; evaluating the casts one CTE removed from the collection is pure SQL."""
    base = name[len(NAMES.prefix):]
    table, _, field = base.partition("__")
    node_names = {nt.name for nt in schema.node_types}
    if schema.is_base(table):
        # the base class of the inheritance axis: the UNION ALL of the subclasses' views
        # (their id and base-class columns), or of their element views of a LIST base field
        if field:
            return f"{name} AS (" + " UNION ALL ".join(f"SELECT owner, v FROM {NAMES.child(nt.name, field)}" for nt in schema.node_types) + ")"
        cols = ", ".join(["id"] + [f.name for f in schema.base.fields])
        return f"{name} AS (" + " UNION ALL ".join(f"SELECT {cols} FROM {NAMES.table(nt.name)}" for nt in schema.node_types) + ")"
    if table in node_names:
        fields = schema.query_fields(table)
        if field:
            return f"{name} AS (SELECT owner, TO_BIGINT(v) AS v FROM (SELECT {path('id')} AS owner, \"x\" AS v FROM {table} UNNEST {path(field)} AS \"x\") z)"
        return _main_view(name, [("id", None)] + [(f.name, f.type) for f in fields], table)
    et = schema.edge_type(table)
    src = f"{et.from_type} UNNEST {path(et.name)} AS \"l\""
    if field:
        return f"{name} AS (SELECT owner, TO_BIGINT(v) AS v FROM (SELECT {path('eid', 'l')} AS owner, \"y\" AS v FROM {src} UNNEST {path(field, 'l')} AS \"y\") z)"
    cols = [("eid", None, path("eid", "l")), ("src_id", None, path("id")), ("dst_id", None, path("to", "l"))]
    cols += [(f.name, f.type, path(f.name, "l")) for f in et.fields]
    return _main_view(name, [(c[0], c[1]) for c in cols], src, {c[0]: c[2] for c in cols})


def _main_view(name: str, cols: "list", source: str, exprs: "dict | None" = None) -> str:
    """`raw_<name> AS (<collection text>), <name> AS (<casts over raw>)`. `cols` is a list
    of (output column, FieldType or None for a key); `exprs` maps a column to the path it
    reads in the collection (its own name by default)."""
    exprs = exprs or {}
    raw = ", ".join(f"{exprs.get(c, path(c))} AS {c}" for c, _ in cols)
    typed_cols = ", ".join(sql_cast(ftype, c) if ftype is not None else c for c, ftype in cols)
    raw_name = "raw_" + name
    return f"{raw_name} AS (SELECT {raw} FROM {source}), {name} AS (SELECT {typed_cols} FROM {raw_name})"


# a view is `v_` and a generated class or edge type name (`n3`, `e12`) or the base class
# (`N0`), a LIST field's elements `__<field>` after it (never the decoding suffix of an
# alias: `v__json`)
_VIEW_NAME = re.compile(r"\bv_[neN]\d+(?:__[a-z]\d+)?\b")


def with_views(schema: Schema, text: str) -> str:
    """`text` with the definitions of every view it names in front. A view's definition
    is one or two CTEs (`view_definition`), already named. A base-class view (the
    inheritance axis) unites the subclasses' views, which are defined before it."""
    used = set(_VIEW_NAME.findall(text))
    if not used:
        return text
    if schema.base is not None:
        for v in list(used):
            table, _, field = v[len(NAMES.prefix):].partition("__")
            if schema.is_base(table):
                used |= {NAMES.child(nt.name, field) if field else NAMES.table(nt.name) for nt in schema.node_types}
    ordered = sorted(used, key=lambda v: (schema.is_base(v[len(NAMES.prefix):].partition("__")[0]), v))
    defs = ", ".join(view_definition(schema, v) for v in ordered)
    return f"WITH {defs} {text}"


def asks_membership(p) -> bool:
    """Whether a tree asks a LIST membership (CONTAINS, CONTAINSANY, CONTAINSALL), which
    the views render as a subquery over an UNNEST element view."""
    if p is None:
        return False
    for leaf in p.leaves():
        if leaf.op in ("CONTAINS", "CONTAINSANY", "CONTAINSALL"):
            return True
        if leaf.sub is not None and asks_membership(leaf.sub[2]):
            return True
    return False


def _membership_conjunct(p) -> bool:
    """Whether every LIST membership of a tree is a positive conjunct of it: under AND
    only, never under OR or NOT, and never inside a semi-join's subquery."""
    from ganda.core.ast.predicates import Compound

    if isinstance(p, Compound):
        if p.op == "AND":
            return all(_membership_conjunct(c) for c in p.children)
        return not asks_membership(p)
    return p.sub is None or not asks_membership(p.sub[2])


def views_ask(p) -> bool:
    """Whether SQL over the views asks a filter. Not a whole-LIST comparison (the views
    hold no list text to compare), and a LIST membership only as a positive conjunct: the
    Document Store accepts an EXISTS over an element view there, and refuses it under OR
    or NOT or inside a subquery ("unsupported form of subquery with UNNEST", B6; found
    live, campaign-20261009-165805)."""
    if p is None:
        return True
    return not compares_list_text(p) and _membership_conjunct(p)


def compares_list_text(p) -> bool:
    """Whether a tree compares a whole LIST (its text), which the views cannot ask."""
    if p is None:
        return False
    for leaf in p.leaves():
        if leaf.ftype == "list" and (leaf.op in MIRROR or leaf.op in ("IN", "BETWEEN")):
            return True
        if leaf.sub is not None and leaf.sub[2] is not None and compares_list_text(leaf.sub[2]):
            return True
    return False


# --- the document store's own filter ---------------------------------------------------------


def _bool_literal(v) -> str:
    return f"TO_JSON_BOOLEAN({'TRUE' if v else 'FALSE'})"


def _sql_string(v: str) -> str:
    """An NVARCHAR literal (the quote doubled). The collection filter's LIKE takes only a
    constant string on its right-hand side (or TO_NVARCHAR / UPPER / LOWER of one), never
    a parameter, so a LIKE pattern is inlined rather than bound."""
    return "'" + str(v).replace("'", "''") + "'"


def _param(ftype: "str | None", bind: str, value) -> str:
    """A pivot as the document store takes it: cast to the type it compares as."""
    if ftype == "bool" or isinstance(value, bool):
        return _bool_literal(value)
    if ftype == "list":
        return f"PARSE_JSON(:{bind}__json)"
    if ftype == "float" or isinstance(value, float):
        return f"CAST(:{bind}__dec AS DECIMAL)"
    if ftype == "int" or isinstance(value, int):
        return f"CAST(:{bind} AS BIGINT)"
    return f"CAST(:{bind} AS NVARCHAR(5000))"


def _params(ftype: "str | None", bind: str, values: list) -> str:
    """A list of pivots, one cast each (`:p__0`, `:p__1`: the list's elements)."""
    return "(" + ", ".join(_param(ftype, f"{bind}__{k}", v) for k, v in enumerate(values)) + ")"


class Unsupported(ValueError):
    """A filter the collection's own query language cannot state."""


_COMPLEMENT = {"=": "<>", "<>": "=", "<": ">=", ">=": "<", ">": "<=", "<=": ">", "IS NULL": "IS NOT NULL", "IS NOT NULL": "IS NULL"}


def doc_pred(p, alias: str = "", negate: bool = False, keep_not: bool = False) -> str:
    """A tree as the document store's own WHERE over the documents of a collection (or,
    with `alias`, over an unnested element), in the documented filter language: a path
    compared with a value, IS [NOT] NULL, LIKE, FOR ANY ... SATISFIES ... END, AND, OR
    and parentheses. A NOT is pushed down to the leaves (De Morgan and the complements,
    which keep every record's three-valued truth) -- or, with `keep_not`, written as
    `NOT (...)` over its operand, a negated membership as `NOT (FOR ANY ...)`, which the
    store evaluates three-valued as SQL does (D8, D19, D20; the `neg` position); `IN` is
    a disjunction of equalities, `BETWEEN` a conjunction of two comparisons. What has no
    form here -- a semi-join, a negated membership (pushed down), arithmetic over the
    field, a reversed comparison -- raises `Unsupported` (`doc_filter_ok`): the question
    is asked through the views."""
    if isinstance(p, Compound):
        if p.op == "NOT":
            if keep_not:
                return f"NOT ({doc_pred(p.children[0], alias, False, True)})"
            return doc_pred(p.children[0], alias, not negate)
        op = p.op if not negate else ("OR" if p.op == "AND" else "AND")
        return "(" + f" {op} ".join(doc_pred(c, alias, negate, keep_not) for c in p.children) + ")"
    if p.sub is not None or p.arith is not None or p.rev:
        raise Unsupported(p.signature)
    col = path(p.field, alias)
    if p.op in ("CONTAINS", "CONTAINSANY", "CONTAINSALL"):
        if negate:
            raise Unsupported("a negated membership")
        if p.op == "CONTAINS":
            return f'FOR ANY "e" IN {col} SATISFIES "e" = {_param("int", p.bind, p.pivot)} END'
        if p.op == "CONTAINSANY":
            alts = " OR ".join(f'"e" = {_param("int", f"{p.bind}__{k}", v)}' for k, v in enumerate(p.pivot))
            return f'FOR ANY "e" IN {col} SATISFIES ({alts}) END'
        return "(" + " AND ".join(f'FOR ANY "e" IN {col} SATISFIES "e" = {_param("int", f"{p.bind}__{k}", v)} END' for k, v in enumerate(p.pivot)) + ")"
    if p.op in ("IS NULL", "IS NOT NULL"):
        return f"{col} {_COMPLEMENT[p.op] if negate else p.op}"
    if p.op == "LIKE":
        return f"{col} {'NOT LIKE' if negate else 'LIKE'} {_sql_string(p.pivot)}"
    if p.op == "BETWEEN":
        lo, hi = _param(p.ftype, p.bind + "a", p.pivot[0]), _param(p.ftype, p.bind + "b", p.pivot[1])
        return f"({col} < {lo} OR {col} > {hi})" if negate else f"({col} >= {lo} AND {col} <= {hi})"
    if p.op in ("IN", "NOT IN"):
        positive = (p.op == "IN") != negate
        parts = [f"{col} {'=' if positive else '<>'} {_param(p.ftype, f'{p.bind}__{k}', v)}" for k, v in enumerate(p.pivot)]
        return "(" + (" OR " if positive else " AND ").join(parts) + ")"
    op = _COMPLEMENT[p.op] if negate else p.op
    return f"{col} {op} {_param(p.ftype, p.bind, p.pivot)}"


def doc_filter_ok(p, keep_not: bool = False) -> bool:
    """Whether the collection's own query language states the filter (with `keep_not`,
    its NOTs kept as written)."""
    if p is None:
        return True
    try:
        doc_pred(p, keep_not=keep_not)
    except Unsupported:
        return False
    return True


def has_not(p) -> bool:
    """Whether a tree holds a NOT node (what the `neg` position keeps as written)."""
    if not isinstance(p, Compound):
        return False
    return p.op == "NOT" or any(has_not(c) for c in p.children)
