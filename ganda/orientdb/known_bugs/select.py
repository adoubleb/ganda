"""Guards on the SELECT IR: ordered keys, DISTINCT, joins, the transaction door and
`select_known`, the entry the select generator calls.
"""
from __future__ import annotations

from ganda.core.query.ir.select import TxWrite
from ganda.orientdb.known_bugs.java_hash import distinct_collides
from ganda.core.data.model import FieldType, Instance, Schema
from ganda.orientdb.engine.dialect import RANGE_KINDS
from ganda.core.query.ir import predicates
from ganda.core.query.ir.predicates import Predicate
from ganda.core.rules.vocabulary import StringOrder
from ganda.orientdb.rules import assumes, works_around
from ganda.orientdb.rules.catalog import SQL, SQL_INDEX
from ganda.orientdb.known_bugs.predicate import _any_indexed, _column_type, _composite_members, _converts, _field_indexes, _int32, _leaves, _mixed_boxes, _plain, edge_case_known, list_ops_known, node_case_known, o39_predicate


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


def o5_poly_sibling_index(schema: Schema, table: str, p, data_model: str) -> bool:
    """O5's mixed-type face through a SIBLING's index (cov400 seed 5272): the
    base-class query `FROM N0 WHERE @class = :cls AND (f2 = :p)` is planned over
    every subclass's index on `f2`, and a sibling's index on its own `f2` of another
    type converts the constant to its key type -- a date string into a LIST index's
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


_ONE_SIDED = ("<", "<=", ">", ">=")


def _reversed_range_fields(leaves: list) -> set:
    """The fields a two-sided range in `leaves` bounds the wrong way round: a
    `BETWEEN hi AND lo`, or a `>` / `>=` leaf and a `<` / `<=` leaf on one field
    with the lower bound above the upper."""
    from ganda.orientdb.engine.semantics import lt

    def below(a, b) -> bool:
        try:
            return lt(a, b)
        except TypeError:
            return False

    out: set = set()
    lows: dict = {}
    highs: dict = {}
    for leaf in leaves:
        if leaf.key is not None or not _plain(leaf) or leaf.constant is None:
            continue
        if leaf.op == "BETWEEN" and isinstance(leaf.constant, list) and len(leaf.constant) == 2 and None not in leaf.constant:
            if below(leaf.constant[1], leaf.constant[0]):
                out.add(leaf.field)
        elif leaf.op in (">", ">=") and not isinstance(leaf.constant, list):
            lows.setdefault(leaf.field, []).append(leaf.constant)
        elif leaf.op in ("<", "<=") and not isinstance(leaf.constant, list):
            highs.setdefault(leaf.field, []).append(leaf.constant)
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
    from ganda.orientdb.engine.semantics import orient_key

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



def select_shape_known(q, sp, data_model: str, schema: Schema, instance: Instance, present: list) -> "str | None":
    """The findings a SELECT IR concrete syntax's SHAPE is the exact face of, whatever its
    filter: O6 (`max` against the first row of a string ordering: ORDER BY collates)
    and, where the ORDER BY runs over the class and may walk an index, O18 / O23
    (`ordered_keys_known`, `_order_form_o18`; an edge type's own indexes for an
    edge target, the transaction's view of the rows under the door). A subquery
    target always sorts."""
    from ganda.core.query.ir.select import column, hits, view
    from ganda.orientdb.concrete_syntax.query.select import WALK_POSITIONS
    from ganda.orientdb.engine.semantics import orient_key

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
    filter an index serves (the class or the base class as the target); and the inner
    lookup's -- `f op $parent.$current.f` with the enclosing record's value as the
    constant, a null one included, served by an index on `f` in the `base` form, never
    under `jrev`."""
    from ganda.core.query.ir.select import column

    if sp.position in ("poly", "polyin") and o5_poly_join(q, sp, schema):
        return "O5"
    if eff is not None:
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
    or inserted), judged on the filter and on a semi-join's inner predicate over the
    written class (or the base class, which holds its records: cov400, seven
    semi-joins `f IN (SELECT f FROM t WHERE f >= :p)` missed the inserted record)."""
    if eff is None:
        return None
    write = q.tx.write
    written = set() if write == "delete" else {q.tx.field}
    fid = o31_known(schema, q.table, eff, data_model, write, written)
    if fid:
        return fid
    for leaf in predicates.subqueries(eff):
        t2, _g, q2, _scan = leaf.sub
        if q2 is not None and (t2 == q.table or schema.is_base(t2)):
            fid = o31_known(schema, t2, q2, data_model, write, written)
            if fid:
                return fid
    return None


def select_known(q, sp, rewrites, data_model: str, schema: Schema, instance: Instance) -> "str | None":
    """The known bug a SELECT IR concrete syntax on `data_model` is the exact shape of, or
    None. The filter is judged as the concrete syntax filters (with the concrete
    syntax's `IS NOT NULL` conjuncts): through `node_case_known` (the class or
    the base class is the target, and it may be index-served); through `edge_case_known`
    for an edge type (the doc data model's bracket filter: its
    own O13); plus a join's and the transaction
    door's own findings (O39 first there: the read is a `LET $r = (SELECT ...)`),
    and the shape's own findings."""
    from ganda.core.query.ir.select import effective, view

    table = q.table
    eff = effective(q, sp.form, q.where)
    if q.tx and eff is not None and o39_predicate(eff):
        return "O39"
    shape_instance = instance
    if q.tx:
        # the filter's findings over the rows the transaction reads (a write may
        # null a column; its semi-joins' answers too)
        instance = view(q, instance)
        eff = predicates.resolve_subs(eff, instance)
    fid = None
    if q.edge:
        if eff is not None:
            fid = edge_case_known(table, eff, schema, instance).get(data_model)
    elif eff is not None:
        plain = node_case_known(table, eff, schema, instance).get(data_model)
        if sp.position in ("poly", "polyin"):
            # the base-class query is not served by the subclass's LIST index
            # (campaign c100d, seed 1046): the list operators' scan faces apply
            fid = plain or list_ops_known(schema, table, eff, data_model, servable=False) or ("O5" if o5_poly_sibling_index(schema, table, eff, data_model) else None)
        else:
            fid = plain
    if fid is None and q.kind == "join":
        fid = _join_known(q, sp, data_model, schema, instance, eff)
    if fid is None and q.tx:
        fid = _tx_known(q, sp, data_model, schema, eff)
    return fid or select_shape_known(q, sp, data_model, schema, shape_instance, rewrites.present)


works_around("O51")


def _wraps(values: list) -> bool:
    """Whether an Integer-boxed running sum over these values may leave 32 bits in
    some visiting order (O19: `sum` / `avg` add Integers in 32-bit arithmetic)."""
    ints = [v for v in values if isinstance(v, int) and not isinstance(v, bool) and _int32(v)]
    return sum(v for v in ints if v > 0) >= 2**31 or sum(v for v in ints if v < 0) < -(2**31)


def unwound_known(q, schema: Schema, instance: Instance) -> "str | None":
    """The known bug the doc data model's edge-type select over its links unwound into
    rows (`select.render_unwound`, a gated cell) is the exact shape of. The link
    documents are undeclared: no collation (O25: a filter, an order, a group or a
    distinct over a `ci` attribute), and a number boxed by its value -- Integer,
    Long, Double -- where the E class and the junction class declare one type: an
    ORDER BY whose key mixes boxes does not sort (O51; `max` / `min` over the rows
    are right, C347, unlike O15's over an embedded list), a sum of Integers wraps in
    32 bits (O19). The filter is judged as the bracket filter is (`edge_case_known`)."""
    from ganda.core.query.ir.select import column, hits

    if q.where is not None:
        fid = edge_case_known(q.table, q.where, schema, instance).get("doc")
        if fid:
            return fid
        if any(leaf.case_insensitive for leaf in _leaves(q.where)):
            return "O25"
    used = set(q.fields) | ({q.arg} if q.kind == "group" and q.arg else set())
    if q.kind in ("group", "distinct", "page", "keys", "agg") and any(column(schema, q, f).case_insensitive for f in used):
        return "O25"
    rows = hits(q, instance)

    def values(f) -> list:
        return [r.values.get(f) for r in rows if r.values.get(f) is not None]

    if q.kind in ("page", "keys") and any(_mixed_boxes(values(f)) for f in q.fields):
        return "O51"
    if q.kind == "agg" and q.fn in ("sum", "avg", "median") and _wraps(values(q.field)):
        return "O19"
    if q.kind == "group" and q.fn in ("sum", "avg", "median") and q.arg:
        groups: dict = {}
        for r in rows:
            groups.setdefault(tuple(repr(r.values.get(k)) for k in q.fields), []).append(r.values.get(q.arg))
        if any(_wraps([v for v in g if v is not None]) for g in groups.values()):
            return "O19"
    return None
