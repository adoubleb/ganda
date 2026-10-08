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
"""
from __future__ import annotations

import re

from ganda.core.ast.predicates import MIRROR, Compound
from ganda.core.model import FieldType, Schema
from ganda.hana.concrete_syntax.predicate import Names



class _DocNames(Names):
    prefix = "v_"


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


def _view(inner_cols: list, outer_cols: list, source: str) -> str:
    inner = f"SELECT {', '.join(inner_cols)} FROM {source}"
    if [c.split(" AS ")[-1] for c in inner_cols] == outer_cols:
        return inner
    return f"SELECT {', '.join(outer_cols)} FROM ({inner}) z"


def sql_cast(ftype: FieldType, col: str) -> str:
    """A projected field (the collection's text) as its SQL type, in SQL: a number by
    TO_BIGINT / TO_DOUBLE of its text, a boolean by its text."""
    if ftype is FieldType.INT:
        return f"TO_BIGINT({col}) AS {col}"
    if ftype is FieldType.FLOAT:
        return f"TO_DOUBLE({col}) AS {col}"
    if ftype is FieldType.BOOL:
        return f"{boolean_of(col)} AS {col}"
    return col


def _columns(fields, alias: str = "") -> "tuple[list, list]":
    inner = [f"{path(f.name, alias)} AS {f.name}" for f in fields]
    outer = [sql_cast(f.type, f.name) for f in fields]
    return inner, outer


def view_definition(schema: Schema, name: str) -> str:
    """The SQL of one view (`v_n1`, `v_n1__f3`, `v_e1`, `v_e1__f`)."""
    base = name[len(NAMES.prefix):]
    table, _, field = base.partition("__")
    node_names = {nt.name for nt in schema.node_types}
    if table in node_names:
        fields = schema.query_fields(table)
        if field:
            return f"SELECT owner, TO_BIGINT(v) AS v FROM (SELECT {path('id')} AS owner, \"x\" AS v FROM {table} UNNEST {path(field)} AS \"x\") z"
        inner, outer = _columns(fields)
        return _view([f"{path('id')} AS id"] + inner, ["id"] + outer, table)
    et = schema.edge_type(table)
    src = f"{et.from_type} UNNEST {path(et.name)} AS \"l\""
    if field:
        return f"SELECT owner, TO_BIGINT(v) AS v FROM (SELECT {path('eid', 'l')} AS owner, \"y\" AS v FROM {src} UNNEST {path(field, 'l')} AS \"y\") z"
    inner, outer = _columns(et.fields, "l")
    keys = [f"{path('eid', 'l')} AS eid", f"{path('id')} AS src_id", f"{path('to', 'l')} AS dst_id"]
    return _view(keys + inner, ["eid", "src_id", "dst_id"] + outer, src)


# a view is `v_` and a generated class or edge type name (`n3`, `e12`), a LIST field's
# elements `__<field>` after it (never the decoding suffix of an alias: `v__json`)
_VIEW_NAME = re.compile(r"\bv_[ne]\d+(?:__[a-z]\d+)?\b")


def with_views(schema: Schema, text: str) -> str:
    """`text` with the definitions of every view it names in front."""
    used = sorted(set(_VIEW_NAME.findall(text)))
    if not used:
        return text
    defs = ", ".join(f"{v} AS ({view_definition(schema, v)})" for v in used)
    return f"WITH {defs} {text}"


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


def doc_pred(p, alias: str = "", negate: bool = False) -> str:
    """A tree as the document store's own WHERE over the documents of a collection (or,
    with `alias`, over an unnested element), in the documented filter language: a path
    compared with a value, IS [NOT] NULL, LIKE, FOR ANY ... SATISFIES ... END, AND, OR
    and parentheses. A NOT is pushed down to the leaves (De Morgan and the complements,
    which keep every record's three-valued truth); `IN` is a disjunction of equalities,
    `BETWEEN` a conjunction of two comparisons. What has no form here -- a semi-join, a
    negated membership, arithmetic over the field, a reversed comparison -- raises
    `Unsupported` (`doc_filter_ok`): the question is asked through the views."""
    if isinstance(p, Compound):
        if p.op == "NOT":
            return doc_pred(p.children[0], alias, not negate)
        op = p.op if not negate else ("OR" if p.op == "AND" else "AND")
        return "(" + f" {op} ".join(doc_pred(c, alias, negate) for c in p.children) + ")"
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
        return f"{col} {'NOT LIKE' if negate else 'LIKE'} {_param(p.ftype, p.bind, p.pivot)}"
    if p.op == "BETWEEN":
        lo, hi = _param(p.ftype, p.bind + "a", p.pivot[0]), _param(p.ftype, p.bind + "b", p.pivot[1])
        return f"({col} < {lo} OR {col} > {hi})" if negate else f"({col} >= {lo} AND {col} <= {hi})"
    if p.op in ("IN", "NOT IN"):
        positive = (p.op == "IN") != negate
        parts = [f"{col} {'=' if positive else '<>'} {_param(p.ftype, f'{p.bind}__{k}', v)}" for k, v in enumerate(p.pivot)]
        return "(" + (" OR " if positive else " AND ").join(parts) + ")"
    op = _COMPLEMENT[p.op] if negate else p.op
    return f"{col} {op} {_param(p.ftype, p.bind, p.pivot)}"


def doc_filter_ok(p) -> bool:
    """Whether the collection's own query language states the filter."""
    if p is None:
        return True
    try:
        doc_pred(p)
    except Unsupported:
        return False
    return True
