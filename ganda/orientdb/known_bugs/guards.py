"""Known findings, each guarded at its EXACT firing shape and nothing wider (the
Ganda policy since the SurrealDB fuzzer's F1/F4/F8): a concrete syntax that would only
re-report a defect already filed is marked `case.known_bugs[data_model] = "<id>"`, and the
runner withholds that data model and counts the suppression instead of writing a bundle.
`--no-known_bugs-known_bugs` keeps every data model, which is how a filed finding is re-confirmed.

Predicates are trees; every guard looks at the LEAVES (a predicate on an indexed
field inside an `OR` or under a `NOT` fires O2 exactly as a bare one does, probed).

O1 (`issue-drafts/O1-correlated-subquery-null-under-index.md`): a correlated subquery
(`$parent.$current` in a LET or projection subquery) sees a null enclosing record when
the enclosing query is served by an index -- `WHERE <indexed field> ...`, `ORDER BY
<indexed field>`, `WHERE id = :s` through the id index. A class scan answers correctly.
The shape: a CORRELATED concrete syntax whose outer query has a source predicate on a field
that this data model holds an index on -- including `rel:flat`'s uncorrelated `IN (SELECT id
...)` predicates on the junction's always-indexed FK columns next to a correlated LET.
`GROUP BY` on an indexed column does NOT trigger it (probed). A PULLED-UP concrete syntax's
outer query is a subquery result, never index-served: the guard is judged on the
query without its source predicate there.

O2 (`issue-drafts/O2-correlated-subquery-plus-indexed-predicate-npe.md`): a subquery
whose WHERE combines a correlated equality (`x = $parent.$current.y`) with a comparison
on a field the class holds an index on answers `java.lang.NullPointerException` --
whatever the index kind, with a literal or a parameter, for every operator (an `IS
NULL` on the indexed field NPEs in the LET-projection form too, and so does the
comparison inside an `OR`). The shape: a CORRELATED concrete syntax whose correlated subquery
carries a second predicate with a leaf on an indexed field of that subquery's class.

O7 (`issue-drafts/O7-list-equality-on-declared-list-property.md`): `f = [1, 2]` / `IN` /
`<>` against a declared `EMBEDDEDLIST LONG` never matches (the literal's or parameter's
elements are Integers, the stored ones Longs), where `f = []`, `CONTAINS` and the same
comparison on an undeclared embedded list work. Symmetric across data models: only the
reference leg sees it, so every data model is withheld.

O8 (`issue-drafts/O8-is-defined-through-a-link.md`): `in.f IS DEFINED` in a WHERE over
an edge class holds for every row and `IS NOT DEFINED` for none -- the test is applied
to the first path segment. The `graph:edgescan` concrete syntax's target predicate is the
only place a dotted `IS [NOT] DEFINED` is emitted.

O9 (`issue-drafts/O9-bracket-filter-after-a-link-path.md`): a bracket filter on a
collection reached THROUGH a multi-valued link (`e2.target.e3[f3 < 1]`, `out_e2.in.
out_e3[...]`) matches nothing: the path is a list of lists and the filter is applied to
the outer elements. Field access through the same path is mapped element-wise, so only
the `doc:path` and `graph:bag` concrete syntaxes with a predicate beyond hop 1 are affected.

O10 (`issue-drafts/O10-in-list-mixing-int-and-long-through-an-index.md`): `f IN [1,
9223372036854775807]` (a list whose elements parse as Integer and Long) is a
`ClassCastException` when an index on `f` serves it, on every index kind; a scan is
fine -- and so is `f IN [686.657, -909]` on a DOUBLE column (Double and Integer; the
cross-type pivots found it, campaign c60b). The shape: an IN predicate on an indexed
column whose list holds numbers of two boxed types (Integer inside [-2^31, 2^31),
Long outside, Double).

O11 (`issue-drafts/O11-composite-index-lower-bound-returns-null-rows.md`): `f < p` /
`f <= p` served by a COMPOSITE index led by `f` returns the records whose `f` is null
or absent as well; a single-field index and a scan do not. The shape: a `<`/`<=`
predicate on a column that is the lead field of a composite index on that data model, over
a class holding a null/absent value in the column.

O12 (`issue-drafts/O12-double-literal-parsed-as-float.md`): a double literal with more
precision than a float32 (`776.417`) is parsed as a Float, so a scan comparing it with a
stored DOUBLE misses while an index lookup converts and matches. Only the select IR's
`literal` filter (`<data_model>:literal`) renders a number as a literal; it is withheld for a
FLOAT pivot that is not float32-exact.

O13 (`issue-drafts/O13-between-over-embedded-int-with-long-bound.md`): `BETWEEN a AND b`
over an UNDECLARED embedded value stored as an Integer misses it when a bound is a Long
(`946 BETWEEN 946 AND 123456789012345678` is false), where `>= a AND <= b`, the same
BETWEEN on a declared LONG, and all-Integer bounds are right. The doc data model's edge
attributes live in embedded documents: an edge BETWEEN whose bounds and stored values
do not share one int width is withheld there.

O14 (`issue-drafts/O14-null-keys-through-an-index.md`): `f IN (SELECT g ...)` served by
an index on `f` returns the records whose `f` is null or absent when the subquery's
answer holds a null (a scan follows the null rules and excludes them); `f = :p` with
a null parameter through an index does the same, and `f IN [x, null]` is an NPE. The
shape: a semi-join leaf (`INSUB`) on a data model that indexes the filtered column, with a
null among the listed values (`insub_known`).

O15 (`issue-drafts/O15-max-over-embedded-list-mixing-int-widths.md`): `max` / `min` over
an embedded list whose numbers mix Integer and Long (the widths the JSON parser gave
them) is a `ClassCastException`; `sum` and a projection of the list are fine. The
shape: the doc data model's `emax` / `emin` over an INT edge attribute whose stored values
do not share one width.

O16 (`issue-drafts/O16-index-truncates-a-double-key-on-a-long-column.md`): an index
converts a key to the column's type -- a double on a LONG column by truncation (`f =
5.5` matches 5 through the index and nothing on a scan; `>= 5.5` includes 5, `< 5.5`
excludes it through an SB-tree index; a hash index truncates the equality key and
scans a range), a Long on an INTEGER column by WRAPPING (`f = 2^31` matches -2^31,
`f > 2^31` everything but the maximum), a double on a FLOAT column by ROUNDING (`f
= 0.1` matches the stored 0.1f, `f > 0.1` excludes it). The shape, per data_model: a `=` /
`IN` leaf with a pivot the conversion changes (`predicates.lossy_pivot`) on an
INT / INT32 / FLOAT32 column the data model indexes, or a `<` / `<=` / `>` / `>=` such
leaf on one a range-capable index serves.

O17 (`issue-drafts/O17-between-truncates-double-bounds-on-a-long-column.md`): `BETWEEN`
converts its bounds to the column's type -- by truncation on a LONG column, by
wrapping on an INTEGER one (`BETWEEN 1 AND 2^31` answers nothing), by rounding on a
FLOAT one -- on a scan, an index and an embedded Integer alike, where `>= AND <=`
compares as the wider box. Symmetric: only the reference sees it, so every data model is
withheld for a BETWEEN leaf with a lossy bound on such a column; the `>= AND <=`
rewrite stays admitted.

O18 (`issue-drafts/O18-order-by-through-an-index-that-ignores-nulls.md`): an `ORDER
BY` walked from an index created with `ignoreNullValues: true` lacks every record with a
null in a key of the index, ascending and descending alike (3.2.57; on 3.2.56 a DESC
walk answered no rows at all); a composite index is walked for `ORDER BY f, g` or its
prefix, one direction. A second key the index does not cover, mixed directions or a
range predicate route the query through a sort, which is right. Guarded where it is
emitted and a walked record has a null key: the select IR's `max` / `min` through the
`order` form (`_order_form_o18`), and the `keys` kind (`ordered_keys_known`).

O19 (`issue-drafts/O19-sum-over-integers-wraps-in-32-bit-arithmetic.md`): `sum()`
accumulates Integer boxes in 32-bit arithmetic and wraps (`+` promotes to a long, `*`
wraps too). An INTEGER column is never summed beyond int32 (`semantics.sum_is_exact`);
the doc data model's EMBEDDED edge attributes are Integers whenever the JSON value fits an
int, so `esum` over an INT attribute whose values all fit an int32 and whose total
does not is withheld on the doc data model (`o19_shape`).

O20 (`issue-drafts/O20-decimal-compared-with-a-double-through-its-binary-expansion.md`):
a scan compares a DECIMAL column with a double parameter or literal through `new
BigDecimal(double)`, the double's exact binary expansion (`f = 0.1` matches nothing,
`f >= 0.1` excludes the stored 0.1), where an index lookup and a BETWEEN bound
convert through the decimal string (`f = 0.1` matches through the index). The
reference models the scan; the shape, per data model: a `=` / `IN` / `<` / `<=` / `>` /
`>=` leaf with a non-dyadic double pivot on a DECIMAL column the data model indexes
(ranges: a range-capable kind).

O21 (`issue-drafts/O21-in-and-between-ignore-ci-collation-on-a-scan.md`): `IN` and
`BETWEEN` on a `COLLATE ci` property compare code units on a scan (`f IN ['A']`
misses `a`; `IN (SELECT ...)` too) where `=` and `<` compare case-insensitively and
an index -- which stores the lower-cased key -- answers both case-insensitively. The
reference models the scan; the shape, per data model: an `IN` / `BETWEEN` leaf on a ci
column the data model indexes (BETWEEN: a range-capable kind), and a semi-join leaf
(`INSUB`) over a ci column on an indexed data model.

O23 (`issue-drafts/O23-order-by-through-a-composite-index-puts-nulls-first.md`): an
`ORDER BY` walked from a composite index created with `ignoreNullValues: false` puts
the records with a null key FIRST ascending and LAST descending, the reverse of a
sort (and of a single-field index walk). The shape: the select IR's `keys` kind whose keys
are a one-direction prefix of such an index on the data model, over a class with a null in
a key column (`ordered_keys_known`).

O25 (`issue-drafts/O25-ci-collation-ignored-in-bracket-filters-and-link-fields.md`):
a bracket filter (`out('e')[f = :p]`, `e1.target[f = :p]`) and a dotted link field
(`in.f = :p`) compare a `COLLATE ci` property case-sensitively, where the same
predicate over the class, in a MATCH `where:` or in a LET subquery is
case-insensitive. The shape: a `BRACKET` concrete syntax (`syntaxes.BRACKET`) with a
target predicate -- or, for `edgescan`, a source predicate -- holding a `=` / `<>`
/ `<` / `<=` / `>` / `>=` leaf on a ci field.

O27 (`issue-drafts/O27-partial-datetime-strings-compare-as-midnight.md`): a
DATETIME / DATE compared with a string the date format only partially matches (an
ISO `T` separator, a missing seconds field) is compared as the DATE's MIDNIGHT --
the time is silently dropped -- where a write of the same string is rejected. The
shape: any leaf whose pivot is such a string on a date column (every data model; the
reference expects what the string says).

O28 (`issue-drafts/O28-avg-computed-in-the-operands-arithmetic.md`): `avg()` divides
in the operands' arithmetic: over integers it is integer division (`avg(1, 2, 2)` is
1) and wraps with the sum, over DECIMALs it rounds to the sum's scale. The shape:
the select IR's `agg` kind's `avg` over an integer column whose mean is not integral
(every data model);
DECIMAL and double columns are never averaged.

O26 (`issue-drafts/O26-unionall-of-let-subqueries-with-nested-lets-answers-nothing.md`):
`expand(unionall((a), (b)))` of two subqueries that each carry a nested correlated
LET, wrapped in `SELECT id, v FROM (...) WHERE id IN (SELECT ...)`, loses the nested
values (the LET-bound form of the same union answers nothing at all, which is why
no concrete syntax uses it). The shape: the PULLED-UP `rel:flat` of a `both` question whose
parts carry a subquery -- a field projection's nested LET, or a target predicate's
`IN (SELECT ...)` (c100c seed 808) -- (`mark_known_bugs`).

O46 (`issues/O46-binary-equality-scan-vs-index.md`, found by the large-data probe on
3.2.57): `b = <base64 text>` on a BINARY column matches nothing on a scan -- the new
executor's `OQueryOperatorEquals.equals` decodes the text to a byte[] and then compares
the arrays by identity -- while an index on the column answers it (the key is the
decoded bytes, compared by content); `IN` and `<>` follow, and `UPDATE` / `DELETE
... WHERE b = ...` touch records only when an index exists. No guard: the model never
compares a BINARY column (`SCALAR_TYPES` excludes it, `generators.predicates.draw` answers only
the null tests) and never indexes one (`data_generator._without_blob_indexes`); the
probe records the fact.
"""
from __future__ import annotations

import dataclasses


from ganda.orientdb.known_bugs.java_hash import distinct_collides
from ganda.core.query.ir.select import TxWrite
from ganda.core.data.model import FieldType, Instance, Schema
from ganda.orientdb.storage.dialect import HASH_KINDS, RANGE_KINDS
from ganda.orientdb.facade import predicates
from ganda.core.oracle.case import QueryCase
from ganda.core.query.ir.traversal import REACH_PROJECTIONS, TraversalQuery
from ganda.orientdb.facade.predicates import lossy_pivot, Compound, Predicate
from ganda.orientdb.concrete_syntax.traversal import BRACKET, CORRELATED
from ganda.core.rules.vocabulary import StringOrder
from ganda.orientdb.rules import assumes, works_around
from ganda.orientdb.rules.catalog import SQL, SQL_INDEX

def _leaves(p) -> list:
    return predicates.leaves(p)


def _flat_has_correlated_let(q: TraversalQuery) -> bool:
    return len(q.hops) > 1 or q.projection in ("field", "max", "min", "sum")


def _indexed(schema: Schema, table: str, field_name: str, data_model: str, edge: bool = False) -> bool:
    if edge:
        return any(ix.target == "edge" and ix.table == table and ix.field == field_name and ix.applies_to(data_model) for ix in schema.indexes)
    return schema.has_index(table, field_name, data_model)


def _plain(leaf) -> bool:
    """A leaf in the concrete syntax an index can serve: neither reversed (`:p > f`) nor
    arithmetic (`f + :c > :p`) -- both are always scans (probed)."""
    return getattr(leaf, "plain", True)


def _leaf_served(schema: Schema, table: str, leaf, data_model: str, edge: bool = False) -> bool:
    """Whether an index of the data model can serve this leaf: a scalar leaf on an indexed
    field; on a map field only `CONTAINSKEY` through a BY KEY index and
    `CONTAINSVALUE` through a BY VALUE index (a dotted entry is never served); a
    reversed or arithmetic leaf never."""
    if leaf.key is not None or not _plain(leaf):
        return False
    if leaf.op in ("HAS KEY", "HAS VALUE", "VALUES HAS"):
        want = {"HAS KEY": "KEY", "HAS VALUE": "VALUE"}.get(leaf.op)
        return want is not None and any(ix.option("by") == want for ix in schema.node_indexes(table, leaf.field, data_model))
    return _indexed(schema, table, leaf.field, data_model, edge)


def _any_indexed(schema: Schema, table: str, p, data_model: str, edge: bool = False) -> bool:
    return any(_leaf_served(schema, table, leaf, data_model, edge) for leaf in _leaves(p))


works_around("O1")


def o1_shape(q: TraversalQuery, schema: Schema, data_model: str, syntax: str) -> bool:
    if data_model == "rel" and syntax == "flat":
        # the junction's FK columns are always indexed, so `near IN (SELECT id ...)` /
        # `far IN (SELECT id ...)` is index-served; a correlated LET next to it reads
        # a null enclosing row (probed: `v: null` on every row)
        first = q.hops[0]
        ep = first.edge_predicate
        predicated = q.source_predicate is not None or first.target_predicate is not None or (ep is not None and _any_indexed(schema, first.edge_type, ep, data_model, edge=True))
        return predicated and _flat_has_correlated_let(q)
    if (data_model, syntax) not in CORRELATED:
        return False
    p = q.source_predicate
    if p is None:
        return False
    return _any_indexed(schema, q.source_table, p, data_model)


works_around("O2")


def o2_shape(q: TraversalQuery, schema: Schema, data_model: str, syntax: str) -> bool:
    """Which correlated subqueries the concrete syntax emits, and whether one of them carries a
    comparison on an indexed field next to its `$parent.$current` equality. `rel:flat`
    renders its first hop uncorrelated but nests `_rel_sub` for the hops after it."""
    flat = data_model == "rel" and syntax == "flat"
    if (data_model, syntax) not in CORRELATED and not flat:
        return False
    for k, h in enumerate(q.hops):
        if flat and k == 0:
            continue
        et = schema.edge_type(h.edge_type)
        tgt = et.to_type if h.direction == "out" else et.from_type
        ep, tp = h.edge_predicate, h.target_predicate
        if data_model == "rel":
            # the junction subquery: `near = $parent... AND <edge predicate>`
            if ep is not None and _any_indexed(schema, h.edge_type, ep, data_model, edge=True):
                return True
            # the target lookup: `id = $parent... AND <target predicate>`
            if tp is not None and _any_indexed(schema, tgt, tp, data_model):
                return True
        if data_model == "doc" and syntax in ("inpath", "union"):
            # `WHERE e CONTAINS (target = $parent...) AND <target predicate>` over the source class
            if tp is not None and _any_indexed(schema, tgt, tp, data_model):
                return True
    return False


_O7_OPS = ("=", "<>", "IN")


works_around("O7")


def o7_predicate(schema: Schema, table: str, p, edge: bool = False) -> bool:
    """A list-typed comparison against a DECLARED list property. Every edge attribute
    on the doc data model lives in an undeclared embedded document, where the comparison
    works; the graph E class and the junction class declare it, where it does not --
    so an edge-attribute list predicate is asymmetric and the guard names the
    declared data models only. The map face: `CONTAINSVALUE` with an Integer-boxed pivot
    on an `EMBEDDEDMAP LONG` matches nothing on a scan (a BY VALUE index answers)."""
    for leaf in _leaves(p):
        if leaf.op == "HAS VALUE" and isinstance(leaf.pivot, int) and not isinstance(leaf.pivot, bool) and _int32(leaf.pivot):
            return True
        if leaf.op not in _O7_OPS or leaf.key is not None:
            continue
        try:
            if schema.field(table, leaf.field, edge=edge).type is FieldType.LIST:
                return True
        except KeyError:
            continue
    return False


def o7_data_models(q: TraversalQuery, schema: Schema) -> set:
    data_models: set = set()
    if o7_predicate(schema, q.source_table, q.source_predicate):
        data_models |= {"doc", "graph", "rel"}
    data_models |= {s for s in ("doc", "graph", "rel") if list_ops_known(schema, q.source_table, q.source_predicate, s, servable=False)}
    for h in q.hops:
        et = schema.edge_type(h.edge_type)
        tgt = et.to_type if h.direction == "out" else et.from_type
        if o7_predicate(schema, tgt, h.target_predicate):
            data_models |= {"doc", "graph", "rel"}
        data_models |= {s for s in ("doc", "graph", "rel") if list_ops_known(schema, tgt, h.target_predicate, s, servable=False)}
        if o7_predicate(schema, h.edge_type, h.edge_predicate, edge=True):
            data_models |= {"graph", "rel"}
        data_models |= {s for s in ("graph", "rel") if list_ops_known(schema, h.edge_type, h.edge_predicate, s, edge=True, servable=False)}
    return data_models


def _int32(v) -> bool:
    return -(2**31) <= v < 2**31


def _mixed_int_widths(values) -> bool:
    ints = [v for v in values if isinstance(v, int) and not isinstance(v, bool)]
    return len({_int32(v) for v in ints}) > 1


def _boxed(v) -> "str | None":
    """The Java box a JSON number lands in: Integer, Long or Double."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return "Integer" if _int32(v) else "Long"
    if isinstance(v, float):
        return "Double"
    return None


def _mixed_boxes(values) -> bool:
    return len({b for b in map(_boxed, values) if b}) > 1


def _composite_served(schema: Schema, table: str, leaf, leaves: list, data_model: str) -> bool:
    """A leaf on a LATER field of a composite index next to an equality on its lead
    (probed: `f3 = true AND f4 IN [7, 2^40]` through a hash index on (f3, f4) is
    O10's ClassCastException; campaign c100, seed 614)."""
    eq_fields = {x.field for x in leaves if x.op in ("=", "IN") and _plain(x)}
    return _plain(leaf) and any(leaf.field in ix.extra_fields and ix.field in eq_fields for ix in _composite_members(schema, table, data_model))


works_around("O36")


def o36_predicate(schema: Schema, table: str, p, data_model: str) -> bool:
    """O36 (`issue-drafts/O36-between-on-composite-index-lead-ignores-the-second-field.md`):
    `f BETWEEN a AND b AND g op c` served by a range-capable composite index on
    (f, g) answers the lexicographic key range [(a, c), (b, +inf)] -- the condition
    on g is not applied (and a null g is in the range with `ignoreNullValues: false`).
    The shape: a plain BETWEEN leaf on the lead of an SB-tree composite index of the
    data model, a top-level conjunct, next to a plain top-level conjunct on a later field
    of that index, in either order. Not a hash composite (it does not serve the
    range), not `f >= a AND f <= b`, not an equality / IN or a one-sided range on
    the lead (probed)."""
    conj = [leaf for leaf in _conjunct_leaves(p) if _plain(leaf) and leaf.key is None]
    for leaf in conj:
        if leaf.op != "BETWEEN":
            continue
        for ix in _composite_members(schema, table, data_model):
            if ix.field != leaf.field or ix.kind not in RANGE_KINDS:
                continue
            if any(other is not leaf and other.field in ix.extra_fields for other in conj):
                return True
    return False


def o36_shape(q: TraversalQuery, schema: Schema, data_model: str) -> bool:
    if o36_predicate(schema, q.source_table, q.source_predicate, data_model):
        return True
    for h in q.hops:
        et = schema.edge_type(h.edge_type)
        tgt = et.to_type if h.direction == "out" else et.from_type
        if o36_predicate(schema, tgt, h.target_predicate, data_model):
            return True
    return False


def _leaf_indexed(schema: Schema, table: str, leaf, leaves: list, data_model: str, edge: bool) -> bool:
    if leaf.key is not None or not _plain(leaf):
        return False
    if _leaf_served(schema, table, leaf, data_model, edge):
        return True
    return not edge and _composite_served(schema, table, leaf, leaves, data_model)


works_around("O10")


def o10_predicate(schema: Schema, table: str, p, data_model: str, edge: bool = False) -> bool:
    leaves = _leaves(p)
    for leaf in leaves:
        if leaf.op in ("IN", "INSUB") and isinstance(leaf.pivot, list) and _mixed_boxes(leaf.pivot) and _leaf_indexed(schema, table, leaf, leaves, data_model, edge):
            return True
    return False


def o10_shape(q: TraversalQuery, schema: Schema, data_model: str) -> bool:
    if o10_predicate(schema, q.source_table, q.source_predicate, data_model):
        return True
    for h in q.hops:
        et = schema.edge_type(h.edge_type)
        tgt = et.to_type if h.direction == "out" else et.from_type
        if o10_predicate(schema, tgt, h.target_predicate, data_model) or o10_predicate(schema, h.edge_type, h.edge_predicate, data_model, edge=True):
            return True
    return False


def _composite_members(schema: Schema, table: str, data_model: str) -> list:
    """The composite node indexes serving `table` on `data_model`."""
    tables = {table}
    if schema.base is not None:
        tables |= {nt.name for nt in schema.node_types} if schema.is_base(table) else {schema.base.name}
    return [ix for ix in schema.indexes if ix.target == "node" and ix.extra_fields and ix.table in tables and ix.applies_to(data_model)]


works_around("O11")


def o11_predicate(schema: Schema, instance: Instance, table: str, p, data_model: str) -> bool:
    """A `<` / `<=` leaf served by a composite index: on its lead field, or on a
    later field next to an equality on the lead (probed: `f = 1 AND g < 1` through
    an index on (f, g) returns the record whose g is null too)."""
    leaves = _leaves(p)
    eq_fields = {leaf.field for leaf in leaves if leaf.op in ("=", "IN") and leaf.key is None and _plain(leaf)}
    for leaf in leaves:
        if leaf.op not in ("<", "<=") or leaf.key is not None or not _plain(leaf):
            continue
        served = False
        for ix in _composite_members(schema, table, data_model):
            if ix.field == leaf.field:
                served = True
            elif leaf.field in ix.extra_fields and ix.field in eq_fields:
                served = True
        if served and any(n.values.get(leaf.field) is None for n in instance.nodes_of(table)):
            return True
    return False


def o11_shape(q: TraversalQuery, schema: Schema, instance: Instance, data_model: str) -> bool:
    if o11_predicate(schema, instance, q.source_table, q.source_predicate, data_model):
        return True
    for h in q.hops:
        et = schema.edge_type(h.edge_type)
        tgt = et.to_type if h.direction == "out" else et.from_type
        if o11_predicate(schema, instance, tgt, h.target_predicate, data_model):
            return True
    return False


def _pivots(leaf) -> list:
    return leaf.pivot if isinstance(leaf.pivot, list) else [leaf.pivot]


def _column_type(schema: Schema, table: str, field_name: str, edge: bool) -> "FieldType | None":
    try:
        return schema.field(table, field_name, edge=edge).type
    except KeyError:
        return None


def _field_indexes(schema: Schema, table: str, field_name: str, data_model: str, edge: bool) -> list:
    if edge:
        return [ix for ix in schema.indexes if ix.target == "edge" and ix.table == table and ix.field == field_name and ix.applies_to(data_model)]
    return schema.node_indexes(table, field_name, data_model)


def _lossy_key_predicate(schema: Schema, table: str, p, data_model: str, edge: bool, types: tuple) -> bool:
    """A `=` / `IN` / range leaf whose pivot the conversion to the column's type
    changes, on a column of one of `types` an index of the data model serves (as its lead
    field, or as a later field next to an equality on the lead)."""
    leaves = _leaves(p)
    for leaf in leaves:
        if leaf.op not in ("=", "IN", "<", "<=", ">", ">=") or leaf.key is not None or not _plain(leaf):
            continue
        ftype = _column_type(schema, table, leaf.field, edge)
        if ftype not in types:
            continue
        if not any(lossy_pivot(ftype, v) for v in _pivots(leaf)):
            continue
        ixs = _field_indexes(schema, table, leaf.field, data_model, edge)
        if not edge:
            ixs = ixs + [ix for ix in _composite_members(schema, table, data_model) if leaf.field in ix.extra_fields and ix.field in {x.field for x in leaves if x.op in ("=", "IN") and _plain(x)}]
        if leaf.op in ("=", "IN"):
            if ixs:
                return True
        elif any(ix.kind in RANGE_KINDS for ix in ixs):
            return True
    return False


works_around("O16")


def o16_predicate(schema: Schema, table: str, p, data_model: str, edge: bool = False) -> bool:
    return _lossy_key_predicate(schema, table, p, data_model, edge, (FieldType.INT, FieldType.INT32, FieldType.FLOAT32))


works_around("O20")


def o20_predicate(schema: Schema, table: str, p, data_model: str, edge: bool = False) -> bool:
    return _lossy_key_predicate(schema, table, p, data_model, edge, (FieldType.DECIMAL,))


def o20_shape(q: TraversalQuery, schema: Schema, data_model: str) -> bool:
    if o20_predicate(schema, q.source_table, q.source_predicate, data_model):
        return True
    for h in q.hops:
        et = schema.edge_type(h.edge_type)
        tgt = et.to_type if h.direction != "in" else et.from_type
        if o20_predicate(schema, tgt, h.target_predicate, data_model) or o20_predicate(schema, h.edge_type, h.edge_predicate, data_model, edge=True):
            return True
    return False


_CI_OPS = ("=", "<>", "<", "<=", ">", ">=")


def _ci_column(schema: Schema, table: str, field_name: str, edge: bool = False) -> bool:
    try:
        return schema.field(table, field_name, edge=edge).case_insensitive
    except KeyError:
        return False


works_around("O21")


def o21_predicate(schema: Schema, table: str, p, data_model: str) -> bool:
    """An `IN` / `BETWEEN` leaf on a ci column the data model indexes (a hash index answers
    `IN` through the index and scans a `BETWEEN`)."""
    leaves = _leaves(p)
    for leaf in leaves:
        if leaf.op not in ("IN", "INSUB", "BETWEEN") or leaf.key is not None or not _ci_column(schema, table, leaf.field):
            continue
        ixs = schema.node_indexes(table, leaf.field, data_model)
        ixs = ixs + [ix for ix in _composite_members(schema, table, data_model) if leaf.field in ix.extra_fields and ix.field in {x.field for x in leaves if x.op in ("=", "IN") and _plain(x)}]
        if leaf.op in ("IN", "INSUB") and ixs:
            return True
        if leaf.op == "BETWEEN" and any(ix.kind in RANGE_KINDS for ix in ixs):
            return True
    return False


def o21_shape(q: TraversalQuery, schema: Schema, data_model: str) -> bool:
    if o21_predicate(schema, q.source_table, q.source_predicate, data_model):
        return True
    for h in q.hops:
        et = schema.edge_type(h.edge_type)
        tgt = et.to_type if h.direction != "in" else et.from_type
        if o21_predicate(schema, tgt, h.target_predicate, data_model):
            return True
    return False


works_around("O27")


def o27_predicate(schema: Schema, table: str, p, edge: bool = False) -> bool:
    """A date column compared with a partially matching string (compared as the
    date's midnight)."""
    for leaf in _leaves(p):
        ftype = _column_type(schema, table, leaf.field, edge)
        if ftype in (FieldType.DATETIME, FieldType.DATE) and any(lossy_pivot(ftype, v) for v in _pivots(leaf)):
            return True
    return False


def o27_shape(q: TraversalQuery, schema: Schema) -> bool:
    if o27_predicate(schema, q.source_table, q.source_predicate):
        return True
    for h in q.hops:
        et = schema.edge_type(h.edge_type)
        tgt = et.to_type if h.direction != "in" else et.from_type
        if o27_predicate(schema, tgt, h.target_predicate):
            return True
    return False


works_around("O28")


def o28_avg(present: list) -> bool:
    """Whether `avg()` over these integers divides inexactly (the mean is not
    integral: integer division truncates it)."""
    return bool(present) and sum(present) % len(present) != 0


def index_door_known(p, ix, schema: Schema, instance: Instance) -> "str | None":
    """The known bugs the `index:` door shares with an index-served scan of the
    same index: O10 (a mixed-box IN list), O16 / O20 (a lossy key: the door converts
    it too, probed), O21 (the door applies the collation where a scan's IN / BETWEEN
    does not)."""
    leaves = _leaves(p)
    for leaf in leaves:
        if leaf.op == "IN" and isinstance(leaf.pivot, list) and _mixed_boxes(leaf.pivot):
            return "O10"
    for leaf in leaves:
        ftype = _column_type(schema, ix.table, leaf.field, False)
        if any(lossy_pivot(ftype, v) for v in _pivots(leaf)) if ftype else False:
            return "O20" if ftype is FieldType.DECIMAL else ("O27" if ftype in (FieldType.DATETIME, FieldType.DATE) else "O16")
        if leaf.op in ("IN", "BETWEEN") and _ci_column(schema, ix.table, leaf.field):
            return "O21"
    return None


works_around("O25")


def o25_predicate(schema: Schema, table: str, p) -> bool:
    return any(leaf.op in _CI_OPS and _ci_column(schema, table, leaf.field) for leaf in _leaves(p))


def o25_shape(q: TraversalQuery, schema: Schema, data_model: str, syntax: str) -> bool:
    if (data_model, syntax) not in BRACKET:
        return False
    if syntax == "edgescan" and o25_predicate(schema, q.source_table, q.source_predicate):
        return True
    for h in q.hops:
        et = schema.edge_type(h.edge_type)
        tgt = et.to_type if h.direction != "in" else et.from_type
        if o25_predicate(schema, tgt, h.target_predicate):
            return True
    return False


works_around("O18")
works_around("O23")


def ordered_keys_known(schema: Schema, instance: Instance, table: str, keys: list, directions: list, data_model: str, edge: bool = False) -> "str | None":
    """`SELECT ... ORDER BY k1 d1, k2 d2` on `data_model`: when the keys are a
    one-direction prefix of a range index (a single-field one for one key) and a record
    holds a null in a column of the index, O18 if the index ignores nulls, O23 if it is
    a composite one that keeps them."""
    one_direction = len(set(directions)) == 1
    if not one_direction:
        return None
    tables = {table}
    if schema.base is not None and not edge:
        tables |= {nt.name for nt in schema.node_types} if schema.is_base(table) else {schema.base.name}
    rows = instance.edges_of(table) if edge else instance.nodes_of(table)
    target = "edge" if edge else "node"
    for ix in schema.indexes:
        if ix.target != target or ix.table not in tables or not ix.applies_to(data_model) or ix.kind not in RANGE_KINDS:
            continue
        if tuple(keys) != ix.all_fields[: len(keys)]:
            continue
        null_key = any(n.values.get(k) is None for n in rows for k in ix.all_fields)
        if ix.ignore_nulls and null_key:
            return "O18"
        if ix.extra_fields and null_key:
            return "O23"
    return None


def o16_shape(q: TraversalQuery, schema: Schema, data_model: str) -> bool:
    if o16_predicate(schema, q.source_table, q.source_predicate, data_model):
        return True
    for h in q.hops:
        et = schema.edge_type(h.edge_type)
        tgt = et.to_type if h.direction == "out" else et.from_type
        if o16_predicate(schema, tgt, h.target_predicate, data_model) or o16_predicate(schema, h.edge_type, h.edge_predicate, data_model, edge=True):
            return True
    return False


works_around("O17")


def o17_predicate(schema: Schema, table: str, p, edge: bool = False) -> bool:
    for leaf in _leaves(p):
        if leaf.op != "BETWEEN":
            continue
        # a map entry (`m.k BETWEEN ...`) holds the map's value type, a LONG: its
        # bounds truncate alike (remote smoke, seed 5000: `-49.5` admitted `-49`)
        ftype = FieldType.INT if leaf.key is not None else _column_type(schema, table, leaf.field, edge)
        if ftype in (FieldType.INT, FieldType.INT32, FieldType.FLOAT32) and any(lossy_pivot(ftype, v) for v in leaf.pivot):
            return True
    return False


def o17_shape(q: TraversalQuery, schema: Schema) -> bool:
    if o17_predicate(schema, q.source_table, q.source_predicate):
        return True
    for h in q.hops:
        et = schema.edge_type(h.edge_type)
        tgt = et.to_type if h.direction == "out" else et.from_type
        if o17_predicate(schema, tgt, h.target_predicate) or o17_predicate(schema, h.edge_type, h.edge_predicate, edge=True):
            return True
    return False


works_around("O13")


def o13_predicate(instance: Instance, edge_type: str, p) -> bool:
    """An edge BETWEEN whose bounds and stored values do not share one int width."""
    for leaf in _leaves(p):
        if leaf.op != "BETWEEN":
            continue
        ints = [v for v in leaf.pivot if isinstance(v, int) and not isinstance(v, bool)]
        ints += [e.values.get(leaf.field) for e in instance.edges_of(edge_type) if isinstance(e.values.get(leaf.field), int) and not isinstance(e.values.get(leaf.field), bool)]
        if ints and len({_int32(v) for v in ints}) > 1:
            return True
    return False


works_around("O43")


def o43_predicate(schema: Schema, edge_type: str, p) -> bool:
    """O43 (`issue-drafts/O43-...`): `IN` with a list on the LEFT is not list
    membership: a single-element listed list is unwrapped and tested by
    containment (`[-1, 0, 1] IN [[0]]` holds; a multi-element one is list
    equality), in a bracket filter and a plain WHERE alike, declared or not. The
    doc data model's edge attributes live in an undeclared embedded list whose elements
    are Integer-boxed, and there the unwrapped Long is narrowed to int as well
    (`[[2^53]]` matches the lists holding 0); `=` / `<>` are exact (probed). The
    select IR's `rw` rewrite renders a list `=` this way (cov400 seeds 5056, 5202); the
    declared data models' list `IN` is O7's."""
    for leaf in _leaves(p):
        if leaf.op in ("IN", "NOT IN") and leaf.key is None and _column_type(schema, edge_type, leaf.field, True) is FieldType.LIST:
            return True
    return False


def o43_shape(q: TraversalQuery, schema: Schema, data_model: str) -> bool:
    return data_model == "doc" and any(o43_predicate(schema, h.edge_type, h.edge_predicate) for h in q.hops if h.edge_predicate is not None)


works_around("O44")


def o44_dfs_reach(q: TraversalQuery, instance: Instance) -> bool:
    """O44 (`issue-drafts/upstream/O44-...`): `TRAVERSE ... MAXDEPTH k STRATEGY
    DEPTH_FIRST` visits each record once, at the depth it first MEETS it, and does
    not expand a record met at the bound -- so a record reached first by a longer
    walk and again by a shorter one keeps its children unvisited, and the reach set
    misses records within k hops (probed: 11 against BREADTH_FIRST's 12, cov400 seed
    5342). A record is marked when it is PUSHED and the last neighbour is expanded
    first, so the start's own neighbours are always marked at depth 1 (probed). The
    shape (conservative, any neighbour order): some start reaches a record at
    distance >= 2 that has an out-edge by a walk of length L, `dist < L <= k`."""
    if q.projection not in REACH_PROJECTIONS:
        return False
    et, k = q.hops[0].edge_type, q.reach_depth
    out: dict = {}
    for e in instance.edges_of(et):
        out.setdefault(e.from_record, []).append(e.to_record)
    for n in instance.nodes_of(q.source_table):
        dist = {n.record_id: 0}
        layer = {n.record_id}
        for depth in range(1, k + 1):
            layer = {w for u in layer for w in out.get(u, ())}
            for w in layer:
                if w in dist and 2 <= dist[w] < depth and out.get(w):
                    return True
                dist.setdefault(w, depth)
    return False


def o13_shape(q: TraversalQuery, schema: Schema, instance: Instance, data_model: str) -> bool:
    if data_model != "doc":
        return False
    return any(o13_predicate(instance, h.edge_type, h.edge_predicate) for h in q.hops if h.edge_predicate is not None)


works_around("O19")


def o19_shape(q: TraversalQuery, schema: Schema, instance: Instance, data_model: str) -> bool:
    if data_model != "doc" or q.projection != "esum":
        return False
    et = q.hops[-1].edge_type
    if schema.field(et, q.field, edge=True).type not in (FieldType.INT, FieldType.INT32):
        return False
    from ganda.core.query.ir.traversal import _projected_values, final_paths

    for n in instance.nodes_of(q.source_table):
        if q.source_predicate is not None and not predicates.evaluate(n.values, q.source_predicate):
            continue
        vals = [v for v in _projected_values(q, instance, final_paths(q, instance, n.record_id)) if v is not None]
        if vals and all(_int32(v) for v in vals) and not _int32(sum(vals)):
            return True
    return False


works_around("O15")


def o15_shape(q: TraversalQuery, schema: Schema, instance: Instance, data_model: str) -> bool:
    if data_model != "doc" or q.projection not in ("emax", "emin"):
        return False
    et = q.hops[-1].edge_type
    if schema.field(et, q.field, edge=True).type is not FieldType.INT:
        return False
    return _mixed_int_widths([e.values.get(q.field) for e in instance.edges_of(et)])


works_around("O8")


def o8_shape(q: TraversalQuery, data_model: str, syntax: str) -> bool:
    """`edgescan` renders both the target predicate and the source predicate through
    the edge's links (`in.f`, `out.f`)."""
    if data_model != "graph" or syntax != "edgescan":
        return False
    return any(predicates.has_op(p, ("IS PRESENT", "IS ABSENT")) for p in (q.hops[0].target_predicate, q.source_predicate) if p is not None)


works_around("O9")


def o9_shape(q: TraversalQuery, data_model: str, syntax: str) -> bool:
    if (data_model, syntax) not in (("doc", "path"), ("graph", "bag")):
        return False
    return any(h.predicate is not None for h in q.hops[1:])


works_around("O39")


def o39_predicate(p) -> bool:
    """O39 (`issue-drafts/O39-containsvalue-inside-a-value-subquery-npe.md`): a
    `CONTAINSVALUE` leaf anywhere in the WHERE of a subquery whose answer is consumed
    as a value -- `x IN (SELECT ...)`, `NOT IN`, `LET $a = (SELECT ...)` -- is a
    `NullPointerException`, whatever the pivot, the id index or the tree around it
    (under NOT, in an OR); as a FROM target it is fine, and so are `CONTAINSKEY`,
    `m.values() CONTAINS` and `m.k = :p` (probed)."""
    return predicates.has_op_deep(p, ("HAS VALUE",))


def o39_shape(q: TraversalQuery, base: str, syntax: str) -> bool:
    """A LET-subquery concrete syntax, or `rel:flat`'s `IN (SELECT id ...)`, holding a
    CONTAINSVALUE leaf in any of the question's predicates (conservative: the leaf
    may sit outside the subquery)."""
    if (base, syntax) not in CORRELATED and (base, syntax) != ("rel", "flat"):
        return False
    return any(o39_predicate(p) for p in q.predicates)


def _data_model_known(q: TraversalQuery, schema: Schema, instance: Instance, base: str, syntax: str) -> "str | None":
    if o39_shape(q, base, syntax):
        return "O39"
    if o1_shape(q, schema, base, syntax):
        return "O1"
    if o2_shape(q, schema, base, syntax):
        return "O2"
    if o8_shape(q, base, syntax):
        return "O8"
    if o10_shape(q, schema, base):
        return "O10"
    if o11_shape(q, schema, instance, base):
        return "O11"
    if o36_shape(q, schema, base):
        return "O36"
    if o16_shape(q, schema, base):
        return "O16"
    if o20_shape(q, schema, base):
        return "O20"
    if o17_shape(q, schema):
        return "O17"
    if o27_shape(q, schema):
        return "O27"
    if o21_shape(q, schema, base):
        return "O21"
    if o25_shape(q, schema, base, syntax):
        return "O25"
    if o43_shape(q, schema, base):
        return "O43"
    if base == "graph" and syntax == "traverse_dfs" and o44_dfs_reach(q, instance):
        return "O44"
    if o13_shape(q, schema, instance, base):
        return "O13"
    if o15_shape(q, schema, instance, base):
        return "O15"
    if o19_shape(q, schema, instance, base):
        return "O19"
    if o9_shape(q, base, syntax):
        return "O9"
    return None


works_around("O26")


def mark_known_bugs(case: QueryCase, q: TraversalQuery, schema: Schema, instance: Instance) -> QueryCase:
    known_bugs = dict(case.known_bugs)
    o7 = o7_data_models(q, schema)
    q_pulled_up = dataclasses.replace(q, source_predicate=None)
    pulled_up = set(case.params.get("pulled_up", ()))
    for data_model, syntax in case.params.get("concrete_syntax", {}).items():
        base = data_model.split(":", 1)[0]
        if base in o7:
            known_bugs[data_model] = "O7"
            continue
        fid = _data_model_known(q_pulled_up if data_model in pulled_up else q, schema, instance, base, syntax)
        if fid is None and data_model in pulled_up and base == "rel" and syntax == "flat" and q.projection != "nexists" and q.source_predicate is not None:
            # the flat pull-up keeps the source predicate in `id IN (SELECT id FROM
            # src WHERE p)`, a query over the source class an index may serve: its
            # index findings apply (cov400 seeds 5120 / 5203, O16's truncation and
            # wrap on the plan the pull-up was meant to avoid)
            fid = node_case_known(q.source_table, q.source_predicate, schema, instance).get(base)
        if fid is None and data_model in pulled_up and base == "rel" and syntax == "flat" and q.has_both and (q.projection in ("field", "max", "min", "sum") or q.hops[0].target_predicate is not None):
            # the inline union's parts carry a subquery (a nested LET, or a target
            # predicate's `IN (SELECT ...)`) under the pull-up's `IN (subquery)` filter
            fid = "O26"
        if fid:
            known_bugs[data_model] = fid
    return dataclasses.replace(case, known_bugs=known_bugs) if known_bugs != case.known_bugs else case


def float32_exact(v) -> bool:
    import struct

    if isinstance(v, bool) or not isinstance(v, float):
        return True
    try:
        return struct.unpack("f", struct.pack("f", v))[0] == v
    except OverflowError:
        return False


works_around("O12")


def o12_literal(p) -> bool:
    """Whether concrete syntax `p`'s pivots as literals is O12's shape."""
    for leaf in _leaves(p):
        pivots = leaf.pivot if isinstance(leaf.pivot, list) else [leaf.pivot]
        if any(isinstance(v, float) and not float32_exact(v) for v in pivots):
            return True
    return False


def _conjunct_leaves(p) -> list:
    """The leaves at the top of the tree or under a chain of ANDs -- the ones the
    planner can serve from an index; a leaf under a NOT or an OR is not."""
    if p is None:
        return []
    if isinstance(p, Predicate):
        return [p]
    if isinstance(p, Compound) and p.op == "AND":
        return [leaf for c in p.children for leaf in _conjunct_leaves(c)]
    return []


works_around("O5")


def list_ops_known(schema: Schema, table: str, p, data_model: str, edge: bool = False, servable: bool = True) -> "str | None":
    """`CONTAINSANY` / `CONTAINSALL` over a declared `EMBEDDEDLIST LONG`: an
    Integer-boxed element (inside int32) never equals a stored Long on a scan
    (`containsAll` / `containsAny` compare boxes, O7's list face; `CONTAINS`
    coerces), where a list index serving `CONTAINSANY` converts the keys -- and
    answers a ClassCastException for a list mixing the two boxes (O10's face).
    `CONTAINSALL` is never index-served; with `servable` False (a bracket filter,
    a MATCH where, a correlated subquery: the traversal concrete syntaxes) neither is
    `CONTAINSANY`, so every data model is the scan."""
    conjuncts = _conjunct_leaves(p)
    for leaf in _leaves(p):
        if leaf.op not in ("HAS ANY", "HAS ALL") or not isinstance(leaf.pivot, list):
            continue
        if _column_type(schema, table, leaf.field, edge) is not FieldType.LIST:
            continue
        # only a top-level conjunct is served by the list index: under a NOT or an
        # OR the leaf is scanned (benchmark seed 3, `NOT (f CONTAINSANY [..] AND ...)`)
        served = servable and leaf.op == "HAS ANY" and leaf in conjuncts and _leaf_served(schema, table, leaf, data_model, edge)
        if served and any(ix.kind in HASH_KINDS or ix.kind == "DICTIONARY" for ix in _field_indexes(schema, table, leaf.field, data_model, edge)):
            # the planner picks a hash or dictionary list index for CONTAINSANY and
            # cannot evaluate it: a 501 (an O5 face; campaign c100d, seed 1022)
            return "O5"
        boxed = [v for v in leaf.pivot if isinstance(v, int) and not isinstance(v, bool) and _int32(v)]
        if not boxed:
            continue
        if served:
            if _mixed_boxes(leaf.pivot):
                return "O10"
            continue
        return "O7"
    return None


def map_value_index_known(schema: Schema, table: str, p, data_model: str, edge: bool = False, servable: bool = True) -> "str | None":
    """O5's map face: `m CONTAINSVALUE :p` through a `BY VALUE` map index of a hash
    or a DICTIONARY kind is a 501 `Cannot evaluate ... on index` for every pivot
    (probed on all five kinds, cov400 seed 5372); an SB-tree `NOTUNIQUE` / `UNIQUE`
    one answers, `CONTAINSKEY` answers through every kind."""
    if edge or not servable:
        return None
    for leaf in _leaves(p):
        if leaf.op != "HAS VALUE" or leaf.key is not None or not _plain(leaf):
            continue
        if any(ix.option("by") == "VALUE" and (ix.kind in HASH_KINDS or ix.kind == "DICTIONARY") for ix in schema.node_indexes(table, leaf.field, data_model)):
            return "O5"
    return None


_DATE_TYPES = (FieldType.DATETIME, FieldType.DATE)
_NUMBER_TYPES = (FieldType.INT, FieldType.INT32, FieldType.FLOAT, FieldType.FLOAT32, FieldType.DECIMAL)


def _converts(a: "FieldType | None", b: "FieldType | None") -> bool:
    """Whether a value of declared type `b` converts to `a` without throwing (the
    same type, two numbers, two dates); anything else counts as throwing."""
    return a is b or (a in _NUMBER_TYPES and b in _NUMBER_TYPES) or (a in _DATE_TYPES and b in _DATE_TYPES)


def _sibling_types(schema: Schema, table: str, field_name: str) -> list:
    """The declared types of the same-named field on every OTHER subclass."""
    return [f.type for nt in schema.node_types if nt.name != table for f in nt.fields if f.name == field_name]


def o5_sibling_filter(schema: Schema, table: str, p, data_model: str) -> bool:
    """O5's mixed-type face through a base-class index (cov400 seed 5092): a query
    on a SUBCLASS that an index on the abstract base serves fetches the base's
    entries across every subclass and filters them BEFORE `FILTER ITEMS BY CLASS`,
    so a SIBLING's same-named field of another type meets the query's pivot. Probed:
    only a `BETWEEN` under an OR throws there (`b1 = false AND (b1 = true OR f1
    BETWEEN '1978-07-01' AND '2000-02-28')` with a sibling's BOOLEAN `f1`:
    `IllegalArgumentException: Value is not boolean`); the same BETWEEN as a
    top-level conjunct, the other comparisons, no base index or no sibling answer."""
    if schema.base is None or schema.is_base(table) or p is None:
        return False
    base_fields = {ix.field for ix in schema.indexes if ix.target == "node" and ix.table == schema.base.name and ix.applies_to(data_model)}
    leaves = _leaves(p)
    if not any(leaf.field in base_fields and leaf.key is None and _plain(leaf) for leaf in leaves):
        return False
    top = {id(leaf) for leaf in _conjunct_leaves(p)}
    for leaf in leaves:
        if leaf.op != "BETWEEN" or id(leaf) in top or leaf.key is not None:
            continue
        own = _column_type(schema, table, leaf.field, False)
        if any(not _converts(t, own) for t in _sibling_types(schema, table, leaf.field)):
            return True
    return False


def o5_poly_sibling_index(schema: Schema, table: str, p, data_model: str) -> bool:
    """O5's mixed-type face through a SIBLING's index (cov400 seed 5272): the
    base-class query `FROM N0 WHERE @class = :cls AND (f2 = :p)` is planned over
    every subclass's index on `f2`, and a sibling's index on its own `f2` of another
    type converts the pivot to its key type -- a date string into a LIST index's
    Long: `NumberFormatException`. Probed: only when EVERY subclass holds an index
    leading with the field (one class without it, even one lacking the field, and
    the base query scans), and only for `=` / `IN` (a range answers)."""
    if schema.base is None or schema.is_base(table) or p is None:
        return False
    for leaf in _leaves(p):
        if leaf.key is not None or not _plain(leaf) or leaf.op not in ("=", "IN"):
            continue
        per_class = {nt.name: [ix for ix in schema.indexes if ix.target == "node" and ix.table == nt.name and ix.field == leaf.field and ix.applies_to(data_model)] for nt in schema.node_types}
        if not all(per_class.values()):
            continue
        own = _column_type(schema, table, leaf.field, False)
        if any(not _converts(_column_type(schema, name, leaf.field, False), own) for name in per_class if name != table):
            return True
    return False


works_around("O31")


def o31_desc_walk(schema: Schema, table: str, keys: list, directions: list, tx: "TxWrite | None", data_model: str, edge: bool = False) -> bool:
    """O31's walk face (cov400 seeds 5192 / 5287 / 5338 / 5355, 5229's `max` read
    as `ORDER BY f DESC LIMIT 1`): inside a transaction that wrote a non-null value
    to the ordered field (or inserted a record), a single-key DESC walk of an SB-tree
    index answers the written record FIRST (after the nulls), whatever its value.
    Probed: the ASC walk, an id-tiebroken ordering (it sorts), a hash index, no
    index, and a write of null are right; an index ignoring nulls lacks the null
    records on a walk anyway (O18)."""
    if not tx or tx.write not in ("update", "insert") or not keys or not directions or directions[0] != "DESC":
        return False
    if tx.write == "update" and (tx.remove or tx.tv is None or tx.field not in keys):
        return False
    return any(ix.kind in RANGE_KINDS for ix in _field_indexes(schema, table, keys[0], data_model, edge))


works_around("O32")


def o32_predicate(schema: Schema, table: str, p, edge: bool = False) -> bool:
    """A REVERSED leaf (`:p op f`) with a string pivot on a DATETIME / DATE column:
    the engine converts the RIGHT operand to the left one's type, so the date is
    compared as its Java `toString()` (`Mon Jan 01 12:34:56 UTC 2024`) and never
    equals a `yyyy-MM-dd HH:mm:ss` pivot (O32); an epoch pivot compares numerically."""
    for leaf in _leaves(p):
        if getattr(leaf, "rev", False) and isinstance(leaf.pivot, str) and _column_type(schema, table, leaf.field, edge) in (FieldType.DATETIME, FieldType.DATE):
            return True
    return False


_ONE_SIDED = ("<", "<=", ">", ">=")


def _reversed_range_fields(leaves: list) -> set:
    """The fields a two-sided range in `leaves` bounds the wrong way round: a
    `BETWEEN hi AND lo`, or a `>` / `>=` leaf and a `<` / `<=` leaf on one field
    with the lower bound above the upper."""
    from ganda.orientdb.oracle.semantics import lt

    def below(a, b) -> bool:
        try:
            return lt(a, b)
        except TypeError:
            return False

    out: set = set()
    lows: dict = {}
    highs: dict = {}
    for leaf in leaves:
        if leaf.key is not None or not _plain(leaf) or leaf.pivot is None:
            continue
        if leaf.op == "BETWEEN" and isinstance(leaf.pivot, list) and len(leaf.pivot) == 2 and None not in leaf.pivot:
            if below(leaf.pivot[1], leaf.pivot[0]):
                out.add(leaf.field)
        elif leaf.op in (">", ">=") and not isinstance(leaf.pivot, list):
            lows.setdefault(leaf.field, []).append(leaf.pivot)
        elif leaf.op in ("<", "<=") and not isinstance(leaf.pivot, list):
            highs.setdefault(leaf.field, []).append(leaf.pivot)
    for f in set(lows) & set(highs):
        if any(below(h, lo) for lo in lows[f] for h in highs[f]):
            out.add(f)
    return out


def o31_known(schema: Schema, table: str, p, data_model: str, write: str, written: "set | None") -> "str | None":
    """An in-transaction read through a one-sided range (`<`, `<=`, `>`, `>=`) an
    SB-tree index of the data model serves, after the same transaction inserted a record
    of the class or changed the key column of one: the index answers the changed
    records inverted and the inserted ones not at all (O31). A two-sided range,
    an equality, an IN, an ORDER BY walk and a scan are right (probed)."""
    leaves = _leaves(p)
    for field_name in _reversed_range_fields(leaves):
        # a range whose bounds are reversed (`BETWEEN 6 AND 4`, `f >= 6 AND f <= 4`)
        # answers nothing outside a transaction and `IllegalArgumentException:
        # fromKey > toKey` through an SB-tree index inside one, once the
        # transaction has written the column (an update of it, an insert, a
        # delete; campaign c100d, seed 1046)
        touched = write == "delete" or (written is not None and field_name in written) or written is None
        if touched and any(ix.kind in RANGE_KINDS for ix in _field_indexes(schema, table, field_name, data_model, False)):
            return "O31"
    if write not in ("update", "insert"):
        return None
    for leaf in leaves:
        if leaf.op not in _ONE_SIDED or leaf.key is not None or not _plain(leaf):
            continue
        if written is not None and leaf.field not in written:
            continue
        ixs = _field_indexes(schema, table, leaf.field, data_model, False)
        ixs = ixs + [ix for ix in _composite_members(schema, table, data_model) if leaf.field in ix.extra_fields and ix.field in {x.field for x in leaves if x.op in ("=", "IN") and _plain(x)}]
        if any(ix.kind in RANGE_KINDS for ix in ixs):
            return "O31"
    return None


works_around("O14")


def insub_known(schema: Schema, instance: Instance, table: str, p, data_model: str, outer_served: bool = True) -> "str | None":
    """The findings of a tree's SEMI-JOIN leaves (`f IN (SELECT g FROM t2 [WHERE
    q])`, the select IR's `INSUB`): O39 for a CONTAINSVALUE in `q` (a subquery
    consumed as a value, any position); `q`'s own findings as the inner query
    filters (index-served over `t2`, or the scan-side ones where the leaf reads
    `(SELECT FROM t2)`); and where the OUTER query may be index-served, O14 (a null
    among the answered values through an index on `f`: the null-keyed records come
    back). O10 / O21 on the outer lookup are the IN leaf's, judged by
    `o10_predicate` / `o21_predicate`."""
    from ganda.orientdb.concrete_syntax.support import SCAN_SIDE_FINDINGS

    for leaf in predicates.subqueries(p):
        t2, _g, q, scan = leaf.sub
        if q is not None:
            if o39_predicate(q):
                return "O39"
            inner = node_case_known(t2, q, schema, instance).get(data_model)
            if scan:
                inner = list_ops_known(schema, t2, q, data_model, servable=False) or (inner if inner in SCAN_SIDE_FINDINGS else None)
            if inner:
                return inner
        if outer_served and _plain(leaf) and any(v is None for v in leaf.pivot) and schema.has_index(table, leaf.field, data_model):
            return "O14"
    return None


def scan_known(schema: Schema, instance: Instance, table: str, p, data_model: str, edge: bool = False) -> "str | None":
    """The findings a SCAN of the predicate is the exact shape of -- a subquery
    target, the transaction door's `:scan` rewrite -- judged directly, never by
    filtering `node_case_known`'s verdict (which returns the FIRST finding, so an
    index-served one masked a scan-side one: remote campaign cov400 seed 5325, a
    semi-join's O14 hiding a reversed date leaf's O32 on `graph:rev+scan`): the
    semi-joins' own findings with the outer lookup scanned, the list operators
    unserved, O32, O7, O17, O27."""
    if not edge:
        fid = insub_known(schema, instance, table, p, data_model, outer_served=False)
        if fid:
            return fid
    fid = list_ops_known(schema, table, p, data_model, edge=edge, servable=False)
    if fid:
        return fid
    if o32_predicate(schema, table, p, edge):
        return "O32"
    if o7_predicate(schema, table, p, edge) and not (edge and data_model == "doc"):
        return "O7"
    if o17_predicate(schema, table, p, edge):
        return "O17"
    if o27_predicate(schema, table, p, edge):
        return "O27"
    return None


def node_case_known(table: str, p, schema: Schema, instance: Instance) -> dict:
    """Known findings for an identical-text node case with predicate `p` on `table`."""
    known_bugs = {}
    for data_model in ("doc", "graph", "rel"):
        lst = insub_known(schema, instance, table, p, data_model) or list_ops_known(schema, table, p, data_model) or map_value_index_known(schema, table, p, data_model)
        if lst:
            known_bugs[data_model] = lst
        elif o5_sibling_filter(schema, table, p, data_model):
            known_bugs[data_model] = "O5"
        elif o32_predicate(schema, table, p):
            known_bugs[data_model] = "O32"
        elif o7_predicate(schema, table, p):
            known_bugs[data_model] = "O7"
        elif o10_predicate(schema, table, p, data_model):
            known_bugs[data_model] = "O10"
        elif o11_predicate(schema, instance, table, p, data_model):
            known_bugs[data_model] = "O11"
        elif o36_predicate(schema, table, p, data_model):
            known_bugs[data_model] = "O36"
        elif o16_predicate(schema, table, p, data_model):
            known_bugs[data_model] = "O16"
        elif o20_predicate(schema, table, p, data_model):
            known_bugs[data_model] = "O20"
        elif o17_predicate(schema, table, p):
            known_bugs[data_model] = "O17"
        elif o27_predicate(schema, table, p):
            known_bugs[data_model] = "O27"
        elif o21_predicate(schema, table, p, data_model):
            known_bugs[data_model] = "O21"
    return known_bugs


def edge_case_known(edge_type: str, p, schema: Schema, instance: Instance) -> dict:
    """Known findings for an edge-attribute case: the graph E class and the junction
    declare the attributes (O7, O10 through an edge index), the doc data model's embedded
    documents do not (O13)."""
    known_bugs = {}
    for data_model in ("graph", "rel"):
        lst = list_ops_known(schema, edge_type, p, data_model, edge=True)
        if lst:
            known_bugs[data_model] = lst
        elif o32_predicate(schema, edge_type, p, edge=True):
            known_bugs[data_model] = "O32"
        elif o7_predicate(schema, edge_type, p, edge=True):
            known_bugs[data_model] = "O7"
        elif o10_predicate(schema, edge_type, p, data_model, edge=True):
            known_bugs[data_model] = "O10"
        elif o16_predicate(schema, edge_type, p, data_model, edge=True):
            known_bugs[data_model] = "O16"
        elif o17_predicate(schema, edge_type, p, edge=True):
            known_bugs[data_model] = "O17"
    if o43_predicate(schema, edge_type, p):
        known_bugs["doc"] = "O43"
    elif o13_predicate(instance, edge_type, p):
        known_bugs["doc"] = "O13"
    elif o17_predicate(schema, edge_type, p, edge=True):
        known_bugs["doc"] = "O17"
    return known_bugs


def _order_form_o18(schema: Schema, instance: Instance, table: str, fn: str, field_name: str, data_model: str, edge: bool = False) -> bool:
    """`max(f)` / `min(f)` read as the first row of `ORDER BY f DESC|ASC` over the
    class: a walk of a range index that ignores nulls lacks the null records, which `IS
    NOT NULL` excludes -- unless the index is COMPOSITE and a record with a present f
    has a null in another component: that record has no entry and the walk skips it
    (campaign c100e, seed 1049)."""
    cands = _field_indexes(schema, table, field_name, data_model, edge)
    ixs = [ix for ix in cands if ix.kind in RANGE_KINDS and ix.ignore_nulls and ix.field == field_name]
    rows = instance.edges_of(table) if edge else instance.nodes_of(table)
    return any(ix.extra_fields and any(n.values.get(field_name) is not None and any(n.values.get(g) is None for g in ix.extra_fields) for n in rows) for ix in ixs)


works_around("O41")


def o41_values(values: list, key: str, ftype: FieldType, key2: str = "", values2: "list | None" = None, ftype2: "FieldType | None" = None) -> bool:
    """O41 (`issue-drafts/upstream/O41-distinct-returns-duplicates-...`): a DISTINCT
    over these projected values (one column under `key`, or pairs under `key` /
    `key2`) may emit a duplicate row -- the dedup set's bin holding two distinct rows
    of equal hash treeifies and its `compareTo` misses (`java_hash.distinct_collides`).
    Null and absent project alike; a `WHERE f IS NOT NULL` caller passes the present
    values only."""
    from ganda.orientdb.oracle.semantics import orient_key

    seen: dict = {}
    pairs = zip(values, values2) if values2 is not None else ((v, None) for v in values)
    for a, b in pairs:
        row = {key: a, key2: b} if values2 is not None else {key: a}
        seen.setdefault(tuple(orient_key(x) for x in row.values()), row)
    types = {key: ftype, key2: ftype2} if values2 is not None else {key: ftype}
    return distinct_collides(list(seen.values()), types)


# a STRING order: a sort collates, an index walk orders by code unit (O6)
assumes("order.string.order_by", SQL, StringOrder.COLLATION)
assumes("order.string.order_by", SQL_INDEX, StringOrder.UTF16_CODE_UNIT)
works_around("O6")
works_around("O33")


def select_shape_known(q, sp, data_model: str, schema: Schema, instance: Instance, present: list) -> "str | None":
    """The findings a SELECT IR concrete syntax's SHAPE is the exact face of, whatever its
    filter: O28 (`avg` over integers with a non-integral mean), O33 (`count(distinct(
    f))`), O6 (`max` against the first row of a string ordering: ORDER BY collates)
    and, where the ORDER BY runs over the class and may walk an index, O18 / O23
    (`ordered_keys_known`, `_order_form_o18`; an edge type's own indexes for an
    edge target, the transaction's view of the rows under the door). A subquery
    target always sorts."""
    from ganda.core.query.ir.select import column, hits, view
    from ganda.orientdb.concrete_syntax.select import WALK_POSITIONS
    from ganda.orientdb.oracle.semantics import orient_key

    walk = sp.position in WALK_POSITIONS
    inst = view(q, instance)
    if q.kind == "distinct":
        types = {name: column(schema, q, f).type for f, name in zip(q.fields, ("v", "w"))}
        seen: dict = {}
        for n in hits(q, instance):
            vals = tuple(n.values.get(f) for f in q.fields)
            seen.setdefault(tuple(orient_key(v) for v in vals), vals)
        if distinct_collides([dict(zip(("v", "w"), vals)) for vals in seen.values()], types):
            return "O41"
    if q.kind == "agg" and q.fn == "distinct_count" and sp.form == "base":
        if o41_values([n.values.get(q.field) for n in hits(q, instance)], q.field, column(schema, q, q.field).type):
            return "O41"
    if q.kind == "agg":
        if q.fn == "avg" and o28_avg(present):
            return "O28"
        if sp.form == "fn":
            return "O33"
        if sp.form == "order":
            if column(schema, q, q.field).type is FieldType.STRING:
                return "O6"
            if walk and _order_form_o18(schema, inst, q.table, q.fn, q.field, data_model, q.edge):
                return "O18"
            if walk and q.fn == "max" and o31_desc_walk(schema, q.table, [q.field], ["DESC"], q.tx, data_model, q.edge):
                return "O31"
    if q.kind == "keys" and walk and o31_desc_walk(schema, q.table, list(q.fields), list(q.directions), q.tx, data_model, q.edge):
        return "O31"
    if q.kind == "keys" and walk:
        fid = ordered_keys_known(schema, inst, q.table, list(q.fields), list(q.directions), data_model, q.edge)
        if fid is None and sp.position != "table":
            fid = ordered_keys_known(schema, inst, schema.base.name, list(q.fields), list(q.directions), data_model)
        return fid
    return None


_RANGED = ("<", "<=", ">", ">=")


def mixed_compare_throws(left: FieldType, right: FieldType, op: str) -> bool:
    """Whether comparing a stored `left` value with a stored `right` one of another
    declared type is a ClassCastException (O5's mixed-type face, probed field against
    field): a DECIMAL against a LONG under any operator (against an INTEGER, a FLOAT,
    a DOUBLE or a STRING it answers), a DECIMAL against a DATETIME / DATE / BOOLEAN or
    a DATETIME / DATE against a BOOLEAN (either way round) ranged, a stored LIST or
    MAP on the left (ranged probed; the equalities unprobed: counted in), and -- an
    unprobed box against a list -- any left but a LONG or a DOUBLE ranged against a
    LIST or MAP (a DECIMAL throws there, probed)."""
    if left is right:
        return False
    ranged = op in _RANGED
    coll = (FieldType.LIST, FieldType.MAP)
    dates = (FieldType.DATETIME, FieldType.DATE)
    if left in coll:
        return True
    if left is FieldType.DECIMAL:
        if right is FieldType.INT:
            return True
        return (ranged and right in dates + (FieldType.BOOL,)) or right in coll
    if (left in dates and right is FieldType.BOOL) or (left is FieldType.BOOL and right in dates):
        return ranged
    return ranged and right in coll and left not in (FieldType.INT, FieldType.FLOAT)


def o5_poly_join(q, sp, schema: Schema) -> bool:
    """O5's mixed-type face (`issue-drafts/O5-raw-java-exceptions-for-unsupported-
    shapes.md`) through a join over the base class: its LET is evaluated for EVERY
    record of the base before the `@class` filter (LET precedes WHERE), so the
    correlation `f op $parent.$current.f` also compares a SIBLING class's same-named
    field, of another declared type -- on the right in the forward form, on the left
    under `jrev` -- and some pairs of boxes throw (`mixed_compare_throws`; campaign
    seeds 2 and 3 of the parity benchmark: a sibling's LIST and DECIMAL)."""
    own = schema.field(q.table, q.field).type
    for nt in schema.node_types:
        if nt.name == q.table:
            continue
        for f in nt.fields:
            if f.name != q.field or f.type is own:
                continue
            left, right = (f.type, own) if sp.form == "jrev" else (own, f.type)
            if mixed_compare_throws(left, right, q.fn):
                return True
    return False


def _join_known(q, sp, data_model: str, schema: Schema, instance: Instance, eff) -> "str | None":
    """A join's own findings: O1 where the correlated LET sits in a query whose
    filter an index serves (the class or the base class as the target: `nest` and
    `scan` read a subquery's rows, never index-served); and the inner lookup's --
    `f op $parent.$current.f` with the enclosing record's value as the pivot, a null
    one included, served by an index on `f` in the `base` form, never under `jscan` /
    `jrev`."""
    from ganda.core.query.ir.select import column

    if sp.position in ("poly", "polyin") and o5_poly_join(q, sp, schema):
        return "O5"
    if sp.position in ("table", "poly", "polyin") and eff is not None:
        if _any_indexed(schema, q.table, eff, data_model) or (sp.position != "table" and _any_indexed(schema, schema.base.name, eff, data_model)):
            return "O1"
    if sp.form == "base":
        col = column(schema, q, q.field)
        leaf = Predicate("source", q.field, q.fn, "", None, col.type.value, col.case_insensitive)
        return node_case_known(q.table, leaf, schema, instance).get(data_model)
    return None


def _tx_known(q, sp, data_model: str, schema: Schema, eff) -> "str | None":
    """The transaction door's own finding, O31 (a one-sided range, or a reversed
    two-sided one, served by an SB-tree index after the transaction wrote the column
    or inserted), for the positions whose filter an index may serve -- a `scan`
    target never is, and the `index:` door is right inside a transaction (probed) --
    judged on the filter and on a semi-join's inner predicate over the written
    class (or the base class, which holds its records). That inner query reads the
    class itself under every position, `scan` included (cov400: seven `:scan` rewrites
    of `f IN (SELECT f FROM t WHERE f >= :p)` missed the inserted record)."""
    if sp.position == "index" or eff is None:
        return None
    write = q.tx.write
    written = set() if write == "delete" else {q.tx.field}
    if sp.position != "scan":
        fid = o31_known(schema, q.table, eff, data_model, write, written)
        if fid:
            return fid
    for leaf in predicates.subqueries(eff):
        t2, _g, q2, scan = leaf.sub
        if q2 is not None and not scan and (t2 == q.table or schema.is_base(t2)):
            fid = o31_known(schema, t2, q2, data_model, write, written)
            if fid:
                return fid
    return None


def select_known(q, sp, rewrites, data_model: str, schema: Schema, instance: Instance) -> "str | None":
    """The known bug a SELECT IR concrete syntax on `data_model` is the exact shape of, or
    None. The filter is judged as the concrete syntax filters (its rewrite, with the
    concrete syntax's `IS NOT NULL` conjuncts): through `node_case_known` where the class
    is the target (`table`, `nest`, the base class) and it may be index-served; as a
    scan where the target is a subquery (`scan`: only the scan-side findings, and
    the list operators unserved); through `index_door_known` for the `index:` door;
    through `edge_case_known` for an edge type (the doc data model's bracket filter: its
    own O13 / O17); plus O12 for the literal pivots, a join's and the transaction
    door's own findings (O39 first there: the read is a `LET $r = (SELECT ...)`),
    and the shape's own findings."""
    from ganda.core.query.ir.select import effective, view

    table = q.table
    pred = rewrites.filter_pred(q, sp.filter)
    eff = effective(q, sp.form, pred)
    if q.tx and eff is not None and o39_predicate(eff):
        return "O39"
    shape_instance = instance
    if q.tx:
        # the filter's findings over the rows the transaction reads (a write may
        # null a column: O11's null rows; its semi-joins' answers too)
        instance = view(q, instance)
        eff = predicates.resolve_subs(eff, instance)
    fid = None
    if q.edge:
        if eff is not None:
            if data_model != "doc" and sp.position == "scan":
                fid = scan_known(schema, instance, table, eff, data_model, edge=True)
            else:
                fid = edge_case_known(table, eff, schema, instance).get(data_model)
    elif sp.position == "index":
        fid = node_case_known(table, eff, schema, instance).get(data_model) or index_door_known(q.where, rewrites.door[data_model][2], schema, instance)
    elif eff is not None:
        plain = node_case_known(table, eff, schema, instance).get(data_model)
        if sp.position == "scan":
            fid = scan_known(schema, instance, table, eff, data_model)
        elif sp.position in ("poly", "polyin"):
            # the base-class query is not served by the subclass's LIST index
            # (campaign c100d, seed 1046): the list operators' scan faces apply
            fid = plain or list_ops_known(schema, table, eff, data_model, servable=False) or ("O5" if o5_poly_sibling_index(schema, table, eff, data_model) else None)
        else:
            fid = plain
    if fid is None and sp.filter == "literal" and o12_literal(q.where):
        fid = "O12"
    if fid is None and q.kind == "join":
        fid = _join_known(q, sp, data_model, schema, instance, eff)
    if fid is None and q.tx:
        fid = _tx_known(q, sp, data_model, schema, eff)
    return fid or select_shape_known(q, sp, data_model, schema, shape_instance, rewrites.present)


# `IS NOT NULL` through an index is a walk of every non-null key (the arithmetic
# write's own null filter)
_RANGE_OPS = ("<", "<=", ">", ">=", "BETWEEN", "IS NOT NULL")
# the writes that move an index key to a value the statement may meet again
_MOVING_WRITES = ("set", "arith", "merge", "edge_set", "edge_arith")


works_around("O37")


def o37_write(q, sp, data_model: str, pred, schema: Schema) -> bool:
    """O37 (`issue-drafts/O37-update-revisits-the-records-it-moves-through-an-index.md`):
    an UPDATE whose WHERE is served by an SB-tree index walk and whose SET changes a
    field of that index meets the records it moved again -- `SET f = f + 1 WHERE f >
    4` never terminates, `f * 2` doubles until the long wraps, `SET f = 100` counts
    every record twice, a two-sided range walks a record up to its bound, `RETURN
    BEFORE` lists a record twice. Probed on NOTUNIQUE and UNIQUE indexes, either
    null setting, on a vertex class and through an edge index; not through a hash
    index, a scan (a reversed operand), the `nest` target (`id IN (SELECT ...)`
    chooses the records first) or a transaction. The shape (conservative): a moving
    write, auto-commit, on the class or a `sub` target, and a plain top-level
    conjunct served by a range-capable index of the data model that holds the written
    field -- a range on any of its fields, or any comparison on another field of it.
    `SET f = null` / `REMOVE f` / a delete move nothing a walk meets (probed)."""
    if q.op not in _MOVING_WRITES or sp.door == "tx" or sp.target == "nest":
        return False
    if q.edge and data_model == "doc":
        return False
    for leaf in _conjunct_leaves(pred):
        if not _plain(leaf) or leaf.key is not None:
            continue
        if q.edge:
            ixs = [ix for ix in schema.indexes if ix.target == "edge" and ix.table == q.edge_type and ix.applies_to(data_model) and leaf.field in ix.all_fields]
        else:
            ixs = [ix for ix in schema.indexes if ix.target == "node" and ix.applies_to(data_model) and leaf.field in ix.all_fields and ix.table in _poly_tables(schema, q.table)]
        for ix in ixs:
            if ix.kind not in RANGE_KINDS or q.field not in ix.all_fields:
                continue
            if leaf.op in _RANGE_OPS or leaf.field != q.field or ix.extra_fields:
                return True
    return False


def _poly_tables(schema: Schema, table: str) -> set:
    tables = {table}
    if schema.base is not None:
        tables |= {nt.name for nt in schema.node_types} if schema.is_base(table) else {schema.base.name}
    return tables


works_around("O38")


def write_known(q, sp, rewrites, data_model: str, schema: Schema, instance: Instance) -> "str | None":
    """The known bug a WRITE IR concrete syntax on `data_model` would hit, or None. The filter
    is judged as a read of the same predicate would be (a finding there chooses the
    wrong records, and the state diverges from the reference): `node_case_known` /
    `edge_case_known`, the doc data model's bracket filter through its own findings (O13,
    O17) and O25 (a bracket ignores a `ci` collation); then O37."""
    pred = rewrites.pred(q, sp.filter)
    eff = pred
    if q.extra_notnull:
        extra = Predicate("edge" if q.edge else "source", q.field, "IS NOT NULL", "", None)
        eff = extra if pred is None else Compound(pred.on, "AND", (pred, extra))
    fid = None
    if eff is not None:
        if not q.edge:
            fid = node_case_known(q.table, eff, schema, instance).get(data_model)
        elif data_model != "doc":
            fid = edge_case_known(q.edge_type, eff, schema, instance).get(data_model)
        elif sp.form == "bracket":
            fid = edge_case_known(q.edge_type, eff, schema, instance).get("doc") or ("O25" if any(leaf.case_insensitive for leaf in _leaves(eff)) else None)
    if fid is None and o37_write(q, sp, data_model, eff, schema):
        fid = "O37"
    if fid is None and eff is not None and o39_predicate(eff) and (sp.target == "nest" or (q.op == "delete" and data_model == "rel")):
        # the filter inside `id IN (SELECT ...)` (the nest target, the rel data model's
        # cascade) holds a CONTAINSVALUE leaf: O39's NPE
        fid = "O39"
    if fid is None and q.op == "delete" and data_model == "doc" and sp.door == "tx" and sp.form == "deref":
        # O38 (`issue-drafts/O38-...`): in a transaction, a list rewritten through a
        # filter that dereferences its LINKs (`e[target.id NOT IN :gone]`) and a
        # delete of a record a dropped link pointed to fail at COMMIT ("Cannot save
        # document ... no class or cluster defined"); auto-commit, an attribute
        # filter or the relationship-id form are fine (probed)
        fid = "O38"
    return fid
