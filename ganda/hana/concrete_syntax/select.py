"""The concrete syntax catalog of the select IR: HOW each SAP HANA data model asks a
`SelectQuery`. A concrete syntax is three independent choices; every applicable
combination is an equivalent text.

    axis      choice        the text
    filter    plain         the filter as drawn
              rw            an equivalent tree (`rewrites.rewrite`: three-valued De
                            Morgan, `IN [x]` for `= x`, the complement under NOT, ...)
              rev           the reversible leaves' operands swapped (`:p > f`)
              arith         `(f <sym> :c) op :pc`, exact on every stored value
    position  table         rel: `FROM t`; doc: the COLLECTION queried directly (its
                            filter evaluated by the document store); rdf: a `GRAPH`
                            block
              scan          rel, doc views: `FROM (SELECT * FROM t) t`
              nest          rel, doc views: the filter inside the derived table, the
                            kind's projection, aggregate, grouping or ordering outside;
                            rdf: the filter in a subquery
              view          doc: the question in SQL over the views of the collections
                            (`document`), the filter evaluated by SQL
              from          rdf: the graph named by `FROM <g>`
              table_fn      rdf: the SPARQL text inside SQL's `SPARQL_TABLE`
              native        rdf: the filter's unknowns left to SPARQL's error
                            propagation instead of spelled out
              match         graph: the filter in openCypher (`graph.scan`), the kind
                            in SQL over what it projected
              neg           doc: the collection query with the filter's NOTs kept as
                            written (`NOT (...)`, `NOT (FOR ANY ...)`) instead of pushed
                            down to the leaves
              again         rel: the base text a second time (through the plan cache)
              inline        rel: every bound value a SQL constant in the text
                            (`connection.inline_binds`)
              block         rel: the statement inside a SQLScript anonymous block, its
                            binds typed in the header, assigned to a table variable
                            (`connection.sqlscript_block`)
              block_ni      rel: the block with `WITH HINT (NO_INLINE)` on the assignment
    form      base          the kind's own text
              order         `max` / `min` as the first row of an ordering
              fn            a distinct count as `COUNT(*)` over `SELECT DISTINCT`
              jscan         a join's inner relation as a derived table
              jrev          a join's correlation with its operands swapped
              tlp           ids / count as every record EXCEPT the filter's false ones
                            EXCEPT its unknown ones (SQL; MINUS with BIND in SPARQL)
    hint      (none)        the statement as it is
              <profile>     the SQL statement closed by a `WITH HINT (...)` clause steering
                            it to another execution engine or optimizer mode
                            (`hints.PROFILES`): one profile per question, drawn with the
                            rewrites; on every data model whose text is SQL (rdf: the
                            `table_fn` position; doc: the views, never the collection
                            query)

A variant is `<data model>:<tag>`, the tag the non-default choices joined by `+` (form,
filter, position, hint). The base (plain, table, base) is asked of every data model; on the
doc data model it is the collection query where the document store can ask the
question (no semi-join, join, method or median there) and the views otherwise; on the
graph data model it is openCypher where the language states the question
(`_cypher_asks`: no method, avg, median or COUNT(DISTINCT); aggregates over numbers)
and SQL over the `g_` CTEs otherwise (`concrete_syntax.graph`), which `scan` / `nest`
/ `view` ask too.

An edge-type target reads the junction table (`eid AS id`), the doc data model's link
objects (`UNNEST "e" AS "l"` on the source class's collection) and the rdf data model's
relationship resources.

The transaction door (`tx`) wraps every concrete syntax alike (`tx.script`).
"""
from __future__ import annotations

import dataclasses
import itertools
import random
from dataclasses import dataclass, field

from ganda.core.query.ir.predicates import MIRROR, Predicate, fields_of
from ganda.core.query.ir.select import SelectQuery, column, notnull_fields
from ganda.core.data.generator import BASE_CLASS
from ganda.core.data.model import FieldType, Instance, Schema
from ganda.hana.oracle import rewrites as _rewrites
from ganda.hana.concrete_syntax import document, graph, hints, tx as _tx
from ganda.hana.concrete_syntax.methods import METHODS, method_fn, result_type, sparql_text, sql_text  # noqa: F401
from ganda.hana.concrete_syntax.predicate import REL, Names, filter_expr, optionals, sparql_filter, sql
from ganda.hana.concrete_syntax.traversal import agg_proj, guarded, sparql_table
from ganda.hana.storage.dialect import GRAPH_TOKEN, PREFIX_TEXT

FILTERS = ("plain", "rw", "rev", "arith")
POSITIONS = ("table", "scan", "nest", "view", "from", "table_fn", "native", "match", "neg", "again", "inline", "block", "block_ni")
FORMS = ("base", "order", "fn", "jscan", "jrev", "tlp")
DATA_MODELS = ("doc", "rel", "graph", "rdf")
_POSITIONS_OF = {
    # `again`: the base text a second time, after the base has run -- the second execution
    # goes through the plan cache (beside the `plan_cache` hint profile, which ignores it).
    # `inline`: the same statement with every bound value written as a SQL constant
    # (`connection.inline_binds`, probe R22): the parser's typing and the plan cache's
    # key differ from the bound statement's. `block` / `block_ni`: the statement inside
    # a SQLScript anonymous block, its binds typed in the header, assigned to a table
    # variable and selected -- the SQLScript optimizer's path, with statement inlining
    # on, or off under `WITH HINT (NO_INLINE)` (`connection.sqlscript_block`, probe P20)
    "rel": ("table", "scan", "nest", "again", "inline", "block", "block_ni"),
    "doc": ("table", "scan", "nest", "view", "neg"),
    "graph": ("table", "scan", "nest", "view", "match"),
    "rdf": ("table", "nest", "from", "table_fn", "native"),
}
# the largest LIMIT, where an ordering has an OFFSET and no limit of its own
NO_LIMIT = 2147483647
# every group of a GROUP BY holds a solution: a no-op by the standard, which keeps an
# engine from answering an empty group over no solutions (rdflib does)
HAVING = "HAVING (COUNT(*) > 0)"


@dataclass(frozen=True)
class SelectSyntax:
    filter: str = "plain"
    position: str = "table"
    form: str = "base"
    # a plan-steering profile (`hints.PROFILES`), or none
    hint: str = ""

    @property
    def tag(self) -> str:
        parts = [x for x, default in ((self.form, "base"), (self.filter, "plain"), (self.position, "table"), (self.hint, "")) if x != default]
        return "+".join(parts)

    @property
    def axes(self) -> int:
        # the hint is no axis of its own on rdf's `table_fn`, the only SQL path a SPARQL
        # question has: `rdf:table_fn+<profile>` is asked on every question, not in the
        # few random multi-axis combinations
        hinted = self.hint != "" and self.position != "table_fn"
        return len([x for x in (self.form != "base", self.filter != "plain", self.position != "table", hinted) if x])


BASE = SelectSyntax()


@dataclass
class Rewrites:
    rw: object = None
    rev: object = None
    arith: object = None
    present: list = field(default_factory=list)
    # the question's plan-steering profile (`hints.NAMES`), drawn once per question
    hint: str = ""

    def filter_pred(self, q: SelectQuery, name: str):
        return q.where if name == "plain" else getattr(self, name)


def all_syntaxes(hint: str = "") -> list:
    """Every concrete syntax: with `hint`, each one twice, plain and hinted by that one
    profile (a question carries one profile, `Rewrites.hint`)."""
    return [SelectSyntax(f, p, m, h) for h in dict.fromkeys(("", hint)) for m, f, p in itertools.product(FORMS, FILTERS, POSITIONS)]


# --- what the doc data model's collection query can ask ---------------------------------------


def collection_asks(q: SelectQuery, schema: Schema, pred, keep_not: bool = False) -> bool:
    """Whether the document store asks the question itself: no semi-join, no join, no
    method, no median, and a filter whose leaves it renders (with `keep_not`, its NOTs
    kept as written: the `neg` position)."""
    if q.kind in ("join", "method") or (q.kind == "agg" and q.fn == "median"):
        return False
    if q.table == BASE_CLASS and not q.edge:
        # the base class of the inheritance axis is no collection: the views unite them
        return False
    if _string_extremum(q, schema):
        return False
    return document.doc_filter_ok(pred, keep_not)


def _string_extremum(q: SelectQuery, schema: "Schema | None") -> bool:
    """A MAX / MIN over a string: the collection query takes them over numbers only ("max
    supports only numeric values", an error code 129); the views answer it in SQL. Without
    a schema (`base_data_models`) the type is unknown and every MAX / MIN counts as one,
    so a case the views cannot ask is marked partial rather than missing its doc base."""
    if q.fn not in ("max", "min") or q.kind not in ("agg", "group"):
        return False
    return schema is None or _ftype(schema, q, q.field if q.kind == "agg" else q.arg) is FieldType.STRING


def _views_ask(q: SelectQuery, pred) -> bool:
    if not document.views_ask(pred):
        return False
    # a LIST-membership (CONTAINS*) on an EDGE type renders as EXISTS over the element
    # view, which for an edge is a double UNNEST (the link array, then the attribute
    # list); the Document Store refuses that doubly-nested UNNEST view inside a subquery
    # ("unsupported form of subquery with UNNEST"). A node's single-UNNEST element view
    # is fine. The collection's own FOR ANY filter covers the edge shape.
    if q.edge and pred is not None and any(leaf.op in ("CONTAINS", "CONTAINSANY", "CONTAINSALL") for leaf in pred.leaves()):
        return False
    return True


def _rewrite_changes_text(q: SelectQuery, schema: "Schema | None", sp: SelectSyntax, pred, data_model: str) -> bool:
    """Whether a rewritten filter reads differently from the plain one on the engine path
    the position takes. A NOT over a complement comes out as the plain text where the
    renderer pushes NOTs down (the collection filter) or spells three-valued logic out
    (the guarded SPARQL filter), a one-element IN as the equality it is in cypher, a
    semi-join's derived table as the same pattern in SPARQL: the variant would be the
    same statement twice."""
    plain = q.where
    try:
        if data_model == "doc" and sp.position in ("table", "neg"):
            keep = sp.position == "neg"
            return document.doc_pred(pred, keep_not=keep) != document.doc_pred(plain, keep_not=keep)
        if data_model == "graph" and (sp.position == "match" or _cypher_asks(q, schema, sp, pred)):
            return graph.cypher_filter(pred, "t", q.table, schema, graph.Params()) != graph.cypher_filter(plain, "t", q.table, schema, graph.Params())
        if data_model == "rdf":
            native = sp.position == "native"
            return sparql_filter(pred, "n", native=native) != sparql_filter(plain, "n", native=native)
        names = {"doc": document.NAMES, "graph": graph.NAMES}.get(data_model, REL)
        return sql(pred, "t", q.table, names=names) != sql(plain, "t", q.table, names=names)
    except (document.Unsupported, graph.Unsupported):
        return True


def _string_pivots(pred) -> list:
    """Every string pivot a tree binds, the semi-joins' inner trees included."""
    out: list = []
    for leaf in pred.leaves():
        if leaf.sub is not None:
            if leaf.sub[2] is not None:
                out.extend(_string_pivots(leaf.sub[2]))
            continue
        for v in leaf.pivot if isinstance(leaf.pivot, list) else [leaf.pivot]:
            if isinstance(v, str):
                out.append(v)
    return out


def applicable(q: SelectQuery, schema: Schema, sp: SelectSyntax, rewrites: Rewrites, data_model: str) -> bool:
    if sp.position not in _POSITIONS_OF[data_model]:
        return False
    if sp.filter != "plain" and (q.where is None or getattr(rewrites, sp.filter) is None):
        return False
    pred = rewrites.filter_pred(q, sp.filter)
    if sp.filter != "plain" and not _rewrite_changes_text(q, schema, sp, pred, data_model):
        return False
    if sp.form == "tlp":
        # the set-operation form (probe R23 / D25 / K26): the records a filter keeps are
        # every record EXCEPT the false ones EXCEPT the unknown ones -- ids and counts,
        # over a plain FROM (no nested filter, no cypher scan, no NOT-kept collection text,
        # no error-propagating SPARQL); the collection query has no EXCEPT: the doc views
        if q.kind not in ("ids", "count") or pred is None or sp.position in ("nest", "match", "neg", "native", "again"):
            return False
        if data_model == "doc":
            # the views at the table position already (`view` would be the same text)
            return sp.position in ("table", "scan") and _views_ask(q, pred)
    if sp.form in ("jscan", "jrev") and q.kind != "join":
        return False
    if sp.form == "order" and not (q.kind == "agg" and q.fn in ("max", "min") and rewrites.present):
        return False
    if sp.form == "fn" and not (q.kind == "agg" and q.fn == "distinct_count"):
        return False
    if sp.position == "nest" and q.where is None:
        return False
    if sp.position == "again":
        # the plain base text once more, nothing else about it
        return sp == SelectSyntax(position="again")
    if sp.position == "inline":
        # a string pivot holding a control character (the code-unit successor of a stored
        # value appends U+0000) has no constant the probe vouches for
        return pred is not None and bool(pred.bind_vars) and not any(any(ord(ch) < 32 for ch in v) for v in _string_pivots(pred))
    if sp.position == "block_ni" and sp.hint:
        # one hint clause on the assignment: NO_INLINE, not the question's profile too
        return False
    if sp.hint:
        # the question's one profile, on a statement the clause closes: SQL, and the
        # collection query where the store asks the question (D27); never a SPARQL text
        if sp.hint != rewrites.hint or data_model not in hints.DATA_MODELS:
            return False
        if data_model == "rdf" and sp.position != "table_fn":
            return False
        if data_model == "doc" and sp.position in ("table", "neg"):
            if sp.position == "neg":
                return False
            if collection_asks(q, schema, pred):
                # the hinted collection query: the base's twin (`doc:<profile>`), or a
                # rewritten filter's where the plain collection query asks it
                return dataclasses.replace(sp, hint="") == BASE or applicable(q, schema, dataclasses.replace(sp, hint=""), rewrites, data_model)
            return dataclasses.replace(sp, hint="") == BASE and _views_ask(q, pred)
    if data_model == "doc":
        if sp.filter == "arith" and pred is not None and any(leaf.ftype == "float" for leaf in pred.leaves()):
            # a DECIMAL-stored double does not scale like a double (0.30000000000000004 * 2)
            return False
        if sp.form == "fn" and sp.position in ("table", "neg") and collection_asks(q, schema, pred, sp.position == "neg"):
            # the `fn` distinct count nests a GROUP BY in a subquery; over a COLLECTION the
            # Document Store refuses it ("GROUP BY clause after GROUP BY clause on collection
            # tables") -- the base COUNT(DISTINCT) form and the views cover it
            return False
        if sp.position == "neg":
            # the collection query with its NOTs kept: a tree with one, that the store asks
            return pred is not None and document.has_not(pred) and collection_asks(q, schema, pred, keep_not=True)
        if sp.position == "table":
            # the base is the collection query, or the views where the store cannot ask
            if sp == BASE:
                return collection_asks(q, schema, pred) or _views_ask(q, pred)
            return collection_asks(q, schema, pred)
        if sp.position == "view" and dataclasses.replace(sp, position="table", hint="") == BASE and not collection_asks(q, schema, pred):
            # the base (and its hinted twin) is the views text already: `doc:view`
            # would be the same statement again
            return False
        return _views_ask(q, pred)
    if data_model == "graph":
        if q.tx and not graph.TX_VISIBLE:
            return False
        if sp.position == "match":
            return pred is not None and graph.expressible(pred, schema, q.table)
        if sp.position == "view":
            # the views text differs from the base only where the base is native
            return _cypher_asks(q, schema, sp, pred)
        return True
    if data_model == "rdf" and sp.position == "nest" and q.kind == "join":
        return False
    if data_model == "rdf" and sp.position == "from" and q.kind == "agg" and q.fn == "median":
        # the median is one fixed query (a MEDIAN under GROUP BY on a constant), the
        # position changes nothing in it: `rdf:from` would be the base again
        return False
    if sp.position == "native":
        # the error-propagating filter is a variant only where it reads differently from
        # the BOUND-guarded one (a null test alone is `BOUND(?x)` either way)
        return pred is not None and sparql_filter(pred, "n", native=True) != sparql_filter(pred, "n")
    return True


def base_data_models(q: SelectQuery) -> tuple:
    """The data models the question has a base concrete syntax on: the doc data model
    lacks one where neither its collection query nor its views can ask it (a semi-join
    or a negated membership next to a whole-list equality); the graph data model lacks
    one for a transaction door its engine cannot see into (`graph.TX_VISIBLE`)."""
    models = DATA_MODELS if collection_asks(q, None, q.where) or _views_ask(q, q.where) else ("rel", "graph", "rdf")
    if q.tx and not graph.TX_VISIBLE:
        models = tuple(m for m in models if m != "graph")
    return models


def syntaxes_for(q: SelectQuery, schema: Schema, rewrites: Rewrites, data_model: str) -> list:
    return [sp for sp in all_syntaxes(rewrites.hint) if sp != BASE and applicable(q, schema, sp, rewrites, data_model)]


# --- rendering --------------------------------------------------------------------------------


def render(q: SelectQuery, schema: Schema, sp: SelectSyntax, rewrites: Rewrites, data_model: str) -> str:
    pred = rewrites.filter_pred(q, sp.filter)
    inline, block = sp.position == "inline", sp.position if sp.position in ("block", "block_ni") else ""
    if sp.position in ("again", "inline", "block", "block_ni"):
        sp = dataclasses.replace(sp, position="table")
    if data_model == "rel":
        text = _sql(q, schema, sp, pred, REL)
        if inline:
            from ganda.hana.engine.connection import inline_binds

            text = inline_binds(text, pred.bind_vars)
        if block:
            from ganda.hana.engine.connection import sqlscript_block

            # the question's profile hints the inner SELECT; NO_INLINE sits on the assignment
            if sp.hint:
                text = hints.hinted(text, sp.hint)
            text = sqlscript_block(text, pred.bind_vars if pred is not None else {}, noinline=block == "block_ni")
            return _tx.script(_tx.statements(q.tx, schema, data_model), text) if q.tx else text
    elif data_model == "doc":
        if sp.form != "tlp" and (sp.position == "neg" or (sp.position == "table" and collection_asks(q, schema, pred))):
            text = _collection(q, schema, sp, pred, keep_not=sp.position == "neg")
        else:
            text = document.with_views(schema, _sql(q, schema, sp, pred, document.NAMES))
    elif data_model == "graph":
        if sp.position == "table" and _cypher_asks(q, schema, sp, pred):
            text = _cypher_select(q, schema, sp, pred)
        elif sp.position == "match":
            text = _sql(q, schema, sp, None, graph.NAMES, source=f"(SELECT * FROM {graph.scan(schema, q.table, pred)})")
        else:
            text = _sql(q, schema, sp, pred, graph.NAMES)
        text = graph.with_views(schema, text)
    elif sp.position == "table_fn":
        text = sparql_table(_sparql(q, schema, sp, pred, "table"))
    else:
        text = _sparql(q, schema, sp, pred, sp.position)
    if sp.hint:
        text = hints.hinted(text, sp.hint)
    if q.tx:
        return _tx.script(_tx.statements(q.tx, schema, data_model), text)
    return text


def _ftype(schema: Schema, q: SelectQuery, f: str) -> FieldType:
    return column(schema, q, f).type


def _json_alias(schema: Schema, q: SelectQuery, f: str, alias: str) -> str:
    """A projected column's alias: `__json` for a LIST (its text, decoded by the session)."""
    return f"{alias}__json" if _ftype(schema, q, f) is FieldType.LIST else alias


def _sparql_proj(ftype: "FieldType | None", var: str, alias: str) -> str:
    """A value variable `?var` as a SPARQL SELECT-list item for a value whose field type is
    `ftype` (None when unknown -- a plain projection). A FLOAT is read through `STR(?var)`
    aliased `?alias__float`: HANA serializes an `xsd:double` term to ~6 significant figures
    (issues-draft/H1), but `STR` carries the exact lexical form, which the session parses
    (`__float`). An INT is aliased `?alias__int`: SPARQL_TABLE surfaces a possibly-unbound
    numeric variable as an NVARCHAR column (`"7"` not `7`), which the suffix decodes back; it
    is a no-op on the JSON-term path. A BOOL is aliased `?alias__bool` for the same reason
    (SPARQL_TABLE answers it as `"0"` / `"1"`). A LIST stays its JSON text (`__json`)."""
    if ftype is FieldType.FLOAT:
        return f"(STR(?{var}) AS ?{alias}__float)"
    if ftype is FieldType.INT:
        return f"(?{var} AS ?{alias}__int)"
    if ftype is FieldType.BOOL:
        return f"(?{var} AS ?{alias}__bool)"
    if ftype is FieldType.LIST:
        return f"?{alias}__json"
    return f"?{var}" if var == alias else f"(?{var} AS ?{alias})"


# --- SQL (rel, and the doc data model's views) -------------------------------------------------


def _sql(q: SelectQuery, schema: Schema, sp: SelectSyntax, pred, names: Names, source: "str | None" = None) -> str:
    """The SQL text over the relation `names` gives the target (`source` instead: a
    derived table already filtered, the graph data model's `match` position)."""
    rid = "eid" if q.edge else "id"
    key = rid
    rel = names.table(q.table)
    conds = [sql(pred, "t", q.table, key, names)] if pred is not None else []
    conds += [f"t.{f} IS NOT NULL" for f in notnull_fields(q, "order" if sp.form == "order" else "base")]
    if q.kind == "agg" and q.fn in ("set", "list", "distinct_count"):
        conds.append(f"t.{q.field} IS NOT NULL")
    where = f" WHERE {' AND '.join(dict.fromkeys(conds))}" if conds else ""
    if source is not None:
        frm, after = f"FROM {source} t", where
    elif sp.position == "scan":
        frm, after = f"FROM (SELECT * FROM {rel}) t", where
    elif sp.position == "nest":
        frm, after = f"FROM (SELECT * FROM {rel} t{where}) t", ""
    else:
        frm, after = f"FROM {rel} t", where
    src = frm + after
    kind = q.kind
    if sp.form == "tlp":
        # every record EXCEPT those the filter makes false EXCEPT those it leaves
        # unknown (a CASE over the predicate is NULL there): the records it keeps
        p = conds[0]
        rows = f"SELECT t.{rid} AS id {frm}"
        text = f"{rows} EXCEPT {rows} WHERE NOT ({p}) EXCEPT {rows} WHERE (CASE WHEN {p} THEN 1 WHEN NOT ({p}) THEN 0 END) IS NULL"
        return text if kind == "ids" else f"SELECT COUNT(*) AS v FROM ({text}) z"
    if kind == "ids":
        return f"SELECT t.{rid} AS id {src}"
    if kind == "count":
        return f"SELECT COUNT(*) AS v {src}"
    if kind == "field":
        return f"SELECT t.{rid} AS id, t.{q.field} AS {_json_alias(schema, q, q.field, 'v')} {src}"
    if kind == "method":
        return f"SELECT t.{rid} AS id, {sql_text(q.fn, _ftype(schema, q, q.field), 't.' + q.field)} AS v {src}"
    if kind == "agg":
        f = f"t.{q.field}"
        if sp.form == "order":
            return f"SELECT {f} AS v {src} ORDER BY {f} {'DESC' if q.fn == 'max' else 'ASC'} LIMIT 1"
        if q.fn == "distinct_count":
            if sp.form == "fn":
                return f"SELECT COUNT(*) AS v FROM (SELECT DISTINCT {f} {src}) z"
            return f"SELECT COUNT(DISTINCT {f}) AS v {src}"
        if q.fn == "set":
            return f"SELECT DISTINCT {f} AS v {src}"
        if q.fn == "list":
            return f"SELECT {f} AS v {src}"
        if q.fn == "median":
            # HANA's MEDIAN answers in the argument's type: over integers the mean of the two
            # middle values ROUNDED to an integer ({1, 10} -> 6, half away from zero);
            # PERCENTILE_CONT(0.5) is the statistical median the reference computes, and the
            # scalar subquery answers one NULL row over no present value (MEDIAN would,
            # PERCENTILE_CONT ... WITHIN GROUP alone answers no row)
            return f"SELECT (SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY {f}) {src}) AS v FROM DUMMY"
        return f"SELECT {q.fn.upper()}({f}) AS v {src}"
    if kind == "group":
        keys = ", ".join(f"t.{k} AS {name}" for k, name in zip(q.fields, ("k", "k2")))
        value = "COUNT(*)" if q.fn == "count_star" else f"{q.fn.upper()}(t.{q.arg})"
        return f"SELECT {keys}, {value} AS v {src} GROUP BY {', '.join('t.' + k for k in q.fields)}"
    if kind == "distinct":
        cols = ", ".join(f"t.{f} AS {name}" for f, name in zip(q.fields, ("v", "w")))
        return f"SELECT DISTINCT {cols} {src}"
    if kind == "page":
        d = q.directions[0]
        nulls = "NULLS FIRST" if d == "ASC" else "NULLS LAST"
        return f"SELECT t.{rid} AS id {src} ORDER BY t.{q.field} {d} {nulls}, t.{rid} ASC LIMIT {q.limit} OFFSET {q.skip}"
    if kind == "keys":
        cols = ", ".join(f"t.{k} AS {'ab'[i]}" for i, k in enumerate(q.fields))
        order = ", ".join(f"t.{k} {d} {'NULLS FIRST' if d == 'ASC' else 'NULLS LAST'}" for k, d in zip(q.fields, q.directions))
        return f"SELECT {cols} {src} ORDER BY {order}" + _limit(q)
    if kind == "join":
        f, op = q.field, q.fn
        inner = f"(SELECT * FROM {rel})" if sp.form == "jscan" else rel
        cond = f"t.{f} {MIRROR[op]} m.{f}" if sp.form == "jrev" else f"m.{f} {op} t.{f}"
        if q.arg == "count":
            return f"SELECT t.id AS id, (SELECT COUNT(*) FROM {inner} m WHERE {cond}) AS v {src}"
        return f"SELECT t.id AS id, m.id AS v {frm} JOIN {inner} m ON {cond}{after}"
    raise ValueError(kind)


def _limit(q: SelectQuery) -> str:
    if q.limit:
        return f" LIMIT {q.limit}" + (f" OFFSET {q.skip}" if q.skip else "")
    return f" LIMIT {NO_LIMIT} OFFSET {q.skip}" if q.skip else ""


# --- the doc data model's collection query -----------------------------------------------------

# how the session reads a projected text back (`connection.decode_column`)
_DECODE = {FieldType.INT: "__int", FieldType.FLOAT: "__float", FieldType.BOOL: "__bool", FieldType.LIST: "__json", FieldType.STRING: ""}


def _agg_suffix(fn: str, ftype: FieldType) -> str:
    """An aggregate's answer: the column's own type for `max` / `min`, a number (an int
    when integral) for `sum` / `avg`."""
    if ftype is FieldType.STRING:
        return ""
    return _DECODE[ftype] if fn in ("max", "min") else "__num"


def _collection(q: SelectQuery, schema: Schema, sp: SelectSyntax, pred, keep_not: bool = False) -> str:
    """The question asked of the collection itself (an edge type: of its source class's
    collection, the link array UNNESTed). A projected field is the document store's own
    text, the alias telling the session how to read it (`__int`, `__float`, ...). With
    `keep_not` the filter's NOTs stay as written (the `neg` position)."""
    alias = "l" if q.edge else ""
    if q.edge:
        et = schema.edge_type(q.table)
        source = f"{et.from_type} UNNEST {document.path(et.name)} AS \"l\""
        rid = document.path("eid", "l")
    else:
        source = q.table
        rid = document.path("id")

    def p(f: str) -> str:
        return document.path(f, alias)

    def out(f: str, name: str) -> str:
        return f"{p(f)} AS {name}{_DECODE[_ftype(schema, q, f)]}"

    conds = [document.doc_pred(pred, alias, keep_not=keep_not)] if pred is not None else []
    notnull = list(notnull_fields(q, "order" if sp.form == "order" else "base"))
    if q.kind == "agg" and q.fn in ("set", "list", "distinct_count"):
        notnull.append(q.field)
    conds += [f"{p(f)} IS NOT NULL" for f in dict.fromkeys(notnull)]
    src = f"FROM {source}" + (f" WHERE {' AND '.join(conds)}" if conds else "")
    kind = q.kind
    if kind == "ids":
        return f"SELECT {rid} AS id {src}"
    if kind == "count":
        return f"SELECT COUNT(*) AS v {src}"
    if kind == "field":
        return f"SELECT {rid} AS id, {out(q.field, 'v')} {src}"
    if kind == "agg":
        f = q.field
        ftype = _ftype(schema, q, f)
        if sp.form == "order":
            return f"SELECT {out(f, 'v')} {src} ORDER BY {p(f)} {'DESC' if q.fn == 'max' else 'ASC'} LIMIT 1"
        if q.fn == "count":
            return f"SELECT COUNT({p(f)}) AS v {src}"
        if q.fn == "distinct_count":
            if sp.form == "fn":
                return f"SELECT COUNT(*) AS v FROM (SELECT {p(f)} AS x {src} GROUP BY {p(f)}) z"
            return f"SELECT COUNT(DISTINCT {p(f)}) AS v {src}"
        if q.fn == "set":
            return f"SELECT {out(f, 'v')} {src} GROUP BY {p(f)}"
        if q.fn == "list":
            return f"SELECT {out(f, 'v')} {src}"
        return f"SELECT {q.fn.upper()}({document.typed(ftype, p(f))}) AS v{_agg_suffix(q.fn, ftype)} {src}"
    if kind == "group":
        keys = ", ".join(out(k, name) for k, name in zip(q.fields, ("k", "k2")))
        if q.fn == "count_star":
            value = "COUNT(*) AS v"
        elif q.fn == "count":
            value = f"COUNT({p(q.arg)}) AS v"
        else:
            atype = _ftype(schema, q, q.arg)
            value = f"{q.fn.upper()}({document.typed(atype, p(q.arg))}) AS v{_agg_suffix(q.fn, atype)}"
        return f"SELECT {keys}, {value} {src} GROUP BY {', '.join(p(k) for k in q.fields)}"
    if kind == "distinct":
        cols = ", ".join(out(f, name) for f, name in zip(q.fields, ("v", "w")))
        return f"SELECT {cols} {src} GROUP BY {', '.join(p(f) for f in q.fields)}"
    if kind == "page":
        d = q.directions[0]
        return f"SELECT {rid} AS id {src} ORDER BY {p(q.field)} {d}, {rid} ASC LIMIT {q.limit} OFFSET {q.skip}"
    if kind == "keys":
        cols = ", ".join(out(k, "ab"[i]) for i, k in enumerate(q.fields))
        order = ", ".join(f"{p(k)} {d}" for k, d in zip(q.fields, q.directions))
        return f"SELECT {cols} {src} ORDER BY {order}" + _limit(q)
    raise ValueError(kind)


# --- the graph data model's openCypher ---------------------------------------------------------


def _cypher_asks(q: SelectQuery, schema: Schema, sp: SelectSyntax, pred) -> bool:
    """Whether openCypher states the question itself (the native family): the filter
    has a cypher form, and the kind is one its RETURN expresses -- no method, no avg or
    median, no set-operation form, COUNT(DISTINCT) only as the `fn` form's COUNT over a DISTINCT, MAX / MIN /
    SUM over numbers, an ordering only where the engine orders as the semantics do
    (`graph.NULLS_LIKE_SQL`, `graph.STRING_ORDER_NATIVE`; never a boolean key), a join's
    inner relation never a derived table (`jscan`)."""
    if sp.form in ("jscan", "tlp") or q.kind == "method":
        return False
    if pred is not None and not graph.expressible(pred, schema, q.table):
        return False

    def numeric(f: str) -> bool:
        return _ftype(schema, q, f) in (FieldType.INT, FieldType.FLOAT)

    def boolean(f: str) -> bool:
        return _ftype(schema, q, f) is FieldType.BOOL

    def orderable(f: str, nulls_excluded: bool = False) -> bool:
        ft = _ftype(schema, q, f)
        if ft is FieldType.BOOL or (ft is FieldType.STRING and not graph.STRING_ORDER_NATIVE):
            return False
        return nulls_excluded or graph.NULLS_LIKE_SQL

    # a BOOLEAN attribute is projected only (G10): never inside COUNT (G33), never compared
    if q.kind == "agg":
        if q.fn in ("avg", "median"):
            return False
        if q.fn == "distinct_count":
            return sp.form == "fn"
        if q.fn in ("max", "min"):
            # the `order` form filters the nulls out first, so their placement is moot
            return orderable(q.field, nulls_excluded=True) if sp.form == "order" else numeric(q.field)
        if q.fn == "sum":
            return numeric(q.field)
        if q.fn == "count":
            return not boolean(q.field)
        return True
    if q.kind == "group":
        if q.fn == "count_star":
            return True
        return (q.fn == "count" and not boolean(q.arg)) or numeric(q.arg)
    if q.kind in ("page", "keys"):
        return all(orderable(f) for f in q.fields)
    if q.kind == "join":
        return not boolean(q.field)
    return True


def _cypher_limit(limit: int, skip: int) -> str:
    """`LIMIT n [SKIP m]`: the grammar puts SKIP after LIMIT, so a skip alone rides on
    the largest LIMIT (as `_limit` does with OFFSET)."""
    if not limit and not skip:
        return ""
    return f" LIMIT {limit or NO_LIMIT}" + (f" SKIP {skip}" if skip else "")


def _cypher_select(q: SelectQuery, schema: Schema, sp: SelectSyntax, pred) -> str:
    """The native text: `MATCH (t:n1) WHERE ... RETURN ...` inside `OPENCYPHER_TABLE`,
    the aliases uppercase. SQL wraps only what the language lacks: a sum's NULL over no
    present value (cypher answers 0: the COUNT travels along), an aggregate over no
    match where the engine answers no row (`graph.AGG_EMPTY_ROW`), COUNT(*) over a
    DISTINCT, and a join's count per record (a LEFT JOIN to the records, as the
    traversal's aggregates)."""
    params = graph.Params()
    rid = "t.EID" if q.edge else "t.ID"

    def a(f: str) -> str:
        return f"t.{graph.attr(schema, q.table, f)}"

    conds = [graph.cypher_filter(pred, "t", q.table, schema, params)] if pred is not None else []
    conds += [f"NOT ({a(f)} IS NULL)" for f in notnull_fields(q, "order" if sp.form == "order" else "base")]
    if q.kind == "agg" and q.fn in ("set", "list", "distinct_count"):
        conds.append(f"NOT ({a(q.field)} IS NULL)")
    conds = list(dict.fromkeys(conds))
    head = graph.match_clause(schema, q.table, "t")
    where = f" WHERE {' AND '.join(conds)}" if conds else ""

    def oct(ret: str, tail: str = "", clauses: str = head, cond: str = where) -> str:
        return graph.table_fn(f"{clauses}{cond} RETURN {ret}{tail}", params)

    def one(ret: str, tail: str = "") -> str:
        return f"SELECT * FROM {oct(ret, tail)}"

    kind = q.kind
    if kind == "ids":
        return one(f"{rid} AS ID")
    if kind == "count":
        if graph.AGG_EMPTY_ROW:
            return one(f"COUNT({rid}) AS V")
        return f"SELECT COUNT(*) AS v FROM {oct(f'{rid} AS ID')} z"
    if kind == "field":
        return one(f"{rid} AS ID, {a(q.field)} AS {_json_alias(schema, q, q.field, 'v').upper()}")
    if kind == "agg":
        f = a(q.field)
        if sp.form == "order":
            return one(f"{f} AS V", f" ORDER BY {f} {'DESC' if q.fn == 'max' else 'ASC'} LIMIT 1")
        if q.fn == "distinct_count":
            return f"SELECT COUNT(*) AS v FROM {oct(f'DISTINCT {f} AS V')} z"
        if q.fn == "set":
            return one(f"DISTINCT {f} AS V")
        if q.fn == "list":
            return one(f"{f} AS V")
        if q.fn == "sum":
            return f"SELECT (SELECT CASE WHEN z.N = 0 THEN NULL ELSE z.S END FROM {oct(f'SUM({f}) AS S, COUNT({f}) AS N')} z) AS v FROM DUMMY"
        item = f"COUNT({f}) AS V" if q.fn == "count" else f"{q.fn.upper()}({f}) AS V"
        if graph.AGG_EMPTY_ROW:
            return one(item)
        return f"SELECT (SELECT z.V FROM {oct(item)} z) AS v FROM DUMMY"
    if kind == "group":
        names = ("K", "K2")[: len(q.fields)]
        keys = ", ".join(f"{a(k)} AS {n}" for k, n in zip(q.fields, names))
        if q.fn == "count_star":
            return one(f"{keys}, COUNT({rid}) AS V")
        if q.fn == "count":
            return one(f"{keys}, COUNT({a(q.arg)}) AS V")
        if q.fn == "sum":
            outer = ", ".join(f"z.{n} AS {n.lower()}" for n in names)
            return f"SELECT {outer}, CASE WHEN z.N = 0 THEN NULL ELSE z.S END AS v FROM {oct(f'{keys}, SUM({a(q.arg)}) AS S, COUNT({a(q.arg)}) AS N')} z"
        return one(f"{keys}, {q.fn.upper()}({a(q.arg)}) AS V")
    if kind == "distinct":
        return one("DISTINCT " + ", ".join(f"{a(f)} AS {n}" for f, n in zip(q.fields, ("V", "W"))))
    if kind == "page":
        return one(f"{rid} AS ID", f" ORDER BY {a(q.field)} {q.directions[0]}, {rid} ASC{_cypher_limit(q.limit, q.skip)}")
    if kind == "keys":
        cols = ", ".join(f"{a(k)} AS {'AB'[i]}" for i, k in enumerate(q.fields))
        order = ", ".join(f"{a(k)} {d}" for k, d in zip(q.fields, q.directions))
        return one(cols, f" ORDER BY {order}{_cypher_limit(q.limit, q.skip)}")
    if kind == "join":
        f, op = q.field, q.fn
        m = f"m.{graph.attr(schema, q.table, f)}"
        corr = f"({a(f)} {MIRROR[op]} {m})" if sp.form == "jrev" else f"({m} {op} {a(f)})"
        # the two classes as two patterns of ONE clause (the Cartesian product, filtered)
        clauses = f"{head}, {graph.vertex(schema, q.table, 'm')}"
        cond = f" WHERE {' AND '.join(conds + [corr])}"
        if q.arg == "count":
            paths = oct("t.ID AS ID, COUNT(m.ID) AS V", clauses=clauses, cond=cond)
            return f"SELECT s.ID AS id, COALESCE(p.V, 0) AS v FROM {oct('t.ID AS ID')} s LEFT JOIN {paths} p ON p.ID = s.ID"
        return f"SELECT * FROM {oct('t.ID AS ID, m.ID AS V', clauses=clauses, cond=cond)}"
    raise ValueError(kind)


# --- SPARQL -----------------------------------------------------------------------------------


def _sparql(q: SelectQuery, schema: Schema, sp: SelectSyntax, pred, position: str) -> str:
    cls, idp = (f"c:{q.table}", "p:eid") if q.edge else (f"c:{q.table}", "p:id")
    head = f"?n a {cls} ; {idp} ?id ."
    filt = sparql_filter(pred, "n", native=position == "native")
    notnull = list(notnull_fields(q, "order" if sp.form == "order" else "base"))
    if q.kind == "agg" and q.fn in ("set", "list", "distinct_count"):
        notnull.append(q.field)
    required = " ".join(f"?n p:{f} ?r_{f} ." for f in dict.fromkeys(notnull))
    if position == "nest" and pred is not None:
        records = f"{{ SELECT ?n ?id WHERE {{ {head} {filt} }} }} {required}"
    else:
        records = f"{head} {filt} {required}"
    kind = q.kind
    if sp.form == "tlp":
        # every solution MINUS those the filter makes false MINUS those it leaves an
        # error (unknown): the filter carried through BIND ... FILTER (?t) -- a plain
        # FILTER over COALESCE of an OPTIONAL variable keeps or drops the wrong
        # solutions (A10, probe K26); the guarded expression never raises, so the error
        # partition is empty there and the false one carries the unknowns, which is the
        # same set
        expr = filter_expr(pred, "n")
        opts = optionals(pred, "n")
        left = f"{{ {head.rstrip(' .')} {required}}}"
        part = lambda f: f"MINUS {{ {head} {opts} BIND (({f}) AS ?t) FILTER (?t) }}"  # noqa: E731
        body = f"{left} {part(f'!({expr})')} {part(f'COALESCE(IF({expr}, 1, 0), 2) = 2')}"
        return _graph_wrap("SELECT ?id" if kind == "ids" else "SELECT (COUNT(?id) AS ?v)", body, "", position)

    def opt(f: str, var: str) -> str:
        return f"OPTIONAL {{ ?n p:{f} {var} }}"

    tail = ""
    if kind == "ids":
        select, body = "SELECT ?id", records
    elif kind == "count":
        select, body = "SELECT (COUNT(*) AS ?v)", records
    elif kind == "field":
        ft = _ftype(schema, q, q.field)
        bind = "v__json" if ft is FieldType.LIST else "v"
        select, body = f"SELECT ?id {_sparql_proj(ft, bind, 'v')}", f"{records} {opt(q.field, '?' + bind)}"
    elif kind == "method":
        ft = _ftype(schema, q, q.field)
        expr = sparql_text(q.fn, ft, f"?r_{q.field}")
        select, body = f"SELECT ?id {agg_proj(expr, result_type(q.fn, ft), 'v')}", records
    elif kind == "agg":
        f = q.field
        ft = _ftype(schema, q, f)
        if sp.form == "order":
            select, body = f"SELECT {_sparql_proj(ft, 'v', 'v')}", f"{records} ?n p:{f} ?v ."
            tail = f" ORDER BY {'DESC' if q.fn == 'max' else 'ASC'}(?v) LIMIT 1"
        elif q.fn == "set":
            select, body = f"SELECT DISTINCT {_sparql_proj(ft, 'v', 'v')}", f"{records} ?n p:{f} ?v ."
        elif q.fn == "list":
            select, body = f"SELECT {_sparql_proj(ft, 'v', 'v')}", f"{records} ?n p:{f} ?v ."
        elif q.fn == "distinct_count":
            if sp.form == "fn":
                select, body = "SELECT (COUNT(*) AS ?v)", f"{{ SELECT DISTINCT ?w WHERE {{ {records} ?n p:{f} ?w . }} }}"
            else:
                select, body = "SELECT (COUNT(DISTINCT ?w) AS ?v)", f"{records} ?n p:{f} ?w ."
        elif q.fn == "median":
            # HANA's SPARQL MEDIAN requires a GROUP BY (an internal error without one),
            # though it interpolates an even count the way the reference does. The
            # subquery groups on a bound constant; VALUES ... OPTIONAL ... BIND answers
            # one row even over no present value (the reference's NULL row), so the whole
            # query is built here rather than through `_graph_wrap`. STR carries the median's
            # exact double (issues-draft/H1).
            inner_body = f"GRAPH {GRAPH_TOKEN} {{ {records} ?n p:{f} ?w . BIND(1 AS ?mg) }}"
            inner = f"SELECT (MEDIAN(?w) AS ?m) WHERE {{ {inner_body} }} GROUP BY ?mg"
            return f"{PREFIX_TEXT} SELECT (STR(?v) AS ?v__float) WHERE {{ VALUES ?mone {{ 1 }} OPTIONAL {{ {{ {inner} }} }} BIND(?m AS ?v) }}"
        else:
            fn = q.fn.upper()
            if q.fn == "count":
                item = "(COUNT(?w) AS ?v)"
            elif q.fn in ("max", "min"):
                # max / min answer the field's own type
                item = agg_proj(f"{fn}(?w)", ft, "v")
            else:  # sum, avg: avg is always a double; sum of a float is a double
                item = agg_proj(guarded(fn, "?w"), FieldType.FLOAT if q.fn == "avg" else ft, "v")
            # SUM / AVG over the records holding a value: once a predicate IRI has held a
            # second datatype anywhere in the store, HANA answers unbound for a group with an
            # unbound value in it (H3, RULES.md); a null is skipped by the aggregate anyway
            value = f"?n p:{f} ?w ." if q.fn in ("sum", "avg") else opt(f, "?w")
            select, body = f"SELECT {item}", f"{records} {value}"
    elif kind == "group":
        names = ("k", "k2")[: len(q.fields)]
        opts = " ".join(opt(k, "?" + name) for k, name in zip(q.fields, names))
        keys = " ".join(_sparql_proj(_ftype(schema, q, k), name, name) for k, name in zip(q.fields, names))
        if q.fn == "count_star":
            item = "(COUNT(*) AS ?v)"
        elif q.fn == "count":
            item = "(COUNT(?a) AS ?v)"
        elif q.fn in ("max", "min"):
            item = agg_proj(f"{q.fn.upper()}(?a)", _ftype(schema, q, q.arg), "v")
        else:
            # every group keeps its records (a group of nulls answers unbound), so the
            # value stays OPTIONAL and a null counts as zero inside the SUM (see `agg`)
            atype = _ftype(schema, q, q.arg)
            item = agg_proj(guarded("SUM", "?a", zero="0.0e0" if atype is FieldType.FLOAT else "0"), atype, "v")
        aopt = "" if q.fn == "count_star" else opt(q.arg, "?a")
        select, body = f"SELECT {keys} {item}", f"{records} {opts} {aopt}"
        tail = f" GROUP BY {' '.join('?' + n for n in names)} {HAVING}"
    elif kind == "distinct":
        names = ("v", "w")[: len(q.fields)]
        projs = " ".join(_sparql_proj(_ftype(schema, q, f), n, n) for f, n in zip(q.fields, names))
        select, body = f"SELECT DISTINCT {projs}", f"{records} {' '.join(opt(f, '?' + n) for f, n in zip(q.fields, names))}"
    elif kind == "page":
        d = q.directions[0]
        select, body = "SELECT ?id", f"{records} {opt(q.field, '?o')}"
        tail = f" ORDER BY {d}(?o) ASC(?id) LIMIT {q.limit} OFFSET {q.skip}"
    elif kind == "keys":
        names = ("a", "b")[: len(q.fields)]
        projs = " ".join(_sparql_proj(_ftype(schema, q, f), n, n) for f, n in zip(q.fields, names))
        select, body = f"SELECT {projs}", f"{records} {' '.join(opt(k, '?' + n) for k, n in zip(q.fields, names))}"
        tail = f" ORDER BY {' '.join(f'{d}(?{n})' for n, d in zip(names, q.directions))}"
        if q.limit:
            tail += f" LIMIT {q.limit}"
        if q.skip:
            tail += f" OFFSET {q.skip}"
    elif kind == "join":
        f, op = q.field, q.fn
        sop = "!=" if op == "<>" else op
        mop = "!=" if MIRROR[op] == "<>" else MIRROR[op]
        cond = f"(?f {mop} ?mf)" if sp.form == "jrev" else f"(?mf {sop} ?f)"
        inner = f"?n p:{f} ?f . ?m a c:{q.table} ; p:{f} ?mf ."
        if sp.form == "jscan":
            # the inner records a subquery, the record's own value bound inside the
            # OPTIONAL: its filter reads no variable of the outer pattern
            inner = f"?n p:{f} ?f . {{ SELECT ?m ?mf WHERE {{ ?m a c:{q.table} ; p:{f} ?mf . }} }}"
        if q.arg == "count":
            select = "SELECT ?id (COUNT(?m) AS ?v)"
            body = f"{records} OPTIONAL {{ {inner} FILTER {cond} }}"
            tail = f" GROUP BY ?id {HAVING}"
        else:
            select = "SELECT ?id ?v"
            body = f"{records} {{ {inner} FILTER {cond} }} ?m p:id ?v ."
    else:
        raise ValueError(kind)
    return _graph_wrap(select, body, tail, position)


def _graph_wrap(select: str, body: str, tail: str, position: str) -> str:
    if position == "from":
        return f"{PREFIX_TEXT} {select} FROM {GRAPH_TOKEN} WHERE {{ {body} }}{tail}"
    return f"{PREFIX_TEXT} {select} WHERE {{ GRAPH {GRAPH_TOKEN} {{ {body} }} }}{tail}"


# --- the rewrites of one question ------------------------------------------------------------


def build_rewrites(q: SelectQuery, rng: random.Random, schema: Schema, instance: Instance) -> Rewrites:
    from ganda.core.query.ir.select import present_values, records, view

    rewrites = Rewrites(present=present_values(q, instance) if q.kind == "agg" else [], hint=rng.choice(hints.NAMES))
    p = q.where
    if p is None:
        return rewrites
    rewrites.rw = _rewrites.rewrite(p, rng, "q")
    from ganda.core.query.ir.predicates import reverse

    rewrites.rev = reverse(p)
    if isinstance(p, Predicate) and p.sub is None:
        try:
            ftype = schema.field(q.table, p.field, edge=q.edge).type
        except KeyError:
            ftype = None
        if ftype is not None:
            stored = [n.values.get(p.field) for n in records(q, instance) + (records(q, view(q, instance)) if q.tx else []) if p.field in n.values]
            rewrites.arith = _rewrites.arithmetic(p, rng, "ar", ftype, stored)
    return rewrites


def extra_binds(q: SelectQuery, schema: Schema, rewrites: Rewrites) -> dict:
    bind: dict = {}
    for rewritten in (rewrites.rw, rewrites.rev, rewrites.arith):
        if rewritten is not None:
            bind.update(rewritten.bind_vars)
    if q.tx:
        bind.update(_tx.extra_binds(q.tx, schema))
    return bind


def rewrite_params(rewrites: Rewrites) -> dict:
    params: dict = {"hint": rewrites.hint} if rewrites.hint else {}
    for name in ("rw", "rev", "arith"):
        rewritten = getattr(rewrites, name)
        if rewritten is not None:
            params[name] = rewritten.to_dict()
    return params


__all__ = ["BASE", "METHODS", "method_fn", "fields_of"]
