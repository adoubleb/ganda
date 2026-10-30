"""Guards on one predicate, whichever IR asks it: the leaf and index helpers, and the
leaf shapes of the defects a filter alone triggers (O5, O7, O10, O13, O16, O17, O20,
O21, O27, O36, O39, O43, ...).
"""
from __future__ import annotations

from ganda.core.data.model import FieldType, Instance, Schema
from ganda.orientdb.engine.dialect import HASH_KINDS, RANGE_KINDS
from ganda.core.query.ir import predicates
from ganda.core.query.ir.predicates import Compound, Predicate
from ganda.orientdb.engine.semantics import lossy_constant
from ganda.orientdb.rules import works_around


def _leaves(p) -> list:
    return predicates.leaves(p)


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


_O7_OPS = ("=", "<>", "IN")


works_around("O7")


def o7_predicate(schema: Schema, table: str, p, edge: bool = False) -> bool:
    """A list-typed comparison against a DECLARED list property. Every edge attribute
    on the doc data model lives in an undeclared embedded document, where the comparison
    works; the graph E class and the junction class declare it, where it does not --
    so an edge-attribute list predicate is asymmetric and the guard names the
    declared data models only. The map face: `CONTAINSVALUE` with an Integer-boxed constant
    on an `EMBEDDEDMAP LONG` matches nothing on a scan (a BY VALUE index answers)."""
    for leaf in _leaves(p):
        if leaf.op == "HAS VALUE" and isinstance(leaf.constant, int) and not isinstance(leaf.constant, bool) and _int32(leaf.constant):
            return True
        if leaf.op not in _O7_OPS or leaf.key is not None:
            continue
        try:
            if schema.field(table, leaf.field, edge=edge).type is FieldType.LIST:
                return True
        except KeyError:
            continue
    return False


def _int32(v) -> bool:
    return -(2**31) <= v < 2**31


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
        if leaf.op in ("IN", "INSUB") and isinstance(leaf.constant, list) and _mixed_boxes(leaf.constant) and _leaf_indexed(schema, table, leaf, leaves, data_model, edge):
            return True
    return False


def _composite_members(schema: Schema, table: str, data_model: str) -> list:
    """The composite node indexes serving `table` on `data_model`."""
    tables = {table}
    if schema.base is not None:
        tables |= {nt.name for nt in schema.node_types} if schema.is_base(table) else {schema.base.name}
    return [ix for ix in schema.indexes if ix.target == "node" and ix.extra_fields and ix.table in tables and ix.applies_to(data_model)]


def _constants(leaf) -> list:
    return leaf.constant if isinstance(leaf.constant, list) else [leaf.constant]


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
    """A `=` / `IN` / range leaf whose constant the conversion to the column's type
    changes, on a column of one of `types` an index of the data model serves (as its lead
    field, or as a later field next to an equality on the lead)."""
    leaves = _leaves(p)
    for leaf in leaves:
        if leaf.op not in ("=", "IN", "<", "<=", ">", ">=") or leaf.key is not None or not _plain(leaf):
            continue
        ftype = _column_type(schema, table, leaf.field, edge)
        if ftype not in types:
            continue
        if not any(lossy_constant(ftype, v) for v in _constants(leaf)):
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


def _ci_column(schema: Schema, table: str, field_name: str, edge: bool = False) -> bool:
    try:
        return schema.field(table, field_name, edge=edge).case_insensitive
    except KeyError:
        return False


def o25_embedded(p) -> bool:
    """A filter over the doc data model's link documents, which are undeclared and keep no
    collation: a leaf on a `ci` attribute compares case-sensitively (O25)."""
    return any(leaf.case_insensitive for leaf in _leaves(p))


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


works_around("O27")


def o27_predicate(schema: Schema, table: str, p, edge: bool = False) -> bool:
    """A date column compared with a partially matching string (compared as the
    date's midnight)."""
    for leaf in _leaves(p):
        ftype = _column_type(schema, table, leaf.field, edge)
        if ftype in (FieldType.DATETIME, FieldType.DATE) and any(lossy_constant(ftype, v) for v in _constants(leaf)):
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
        if ftype in (FieldType.INT, FieldType.INT32, FieldType.FLOAT32) and any(lossy_constant(ftype, v) for v in leaf.constant):
            return True
    return False


works_around("O13")


def o13_predicate(instance: Instance, edge_type: str, p) -> bool:
    """An edge BETWEEN whose bounds and stored values do not share one int width."""
    for leaf in _leaves(p):
        if leaf.op != "BETWEEN":
            continue
        ints = [v for v in leaf.constant if isinstance(v, int) and not isinstance(v, bool)]
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


works_around("O39")


def o39_predicate(p) -> bool:
    """O39 (`issue-drafts/O39-containsvalue-inside-a-value-subquery-npe.md`): a
    `CONTAINSVALUE` leaf anywhere in the WHERE of a subquery whose answer is consumed
    as a value -- `x IN (SELECT ...)`, `NOT IN`, `LET $a = (SELECT ...)` -- is a
    `NullPointerException`, whatever the constant, the id index or the tree around it
    (under NOT, in an OR); as a FROM target it is fine, and so are `CONTAINSKEY`,
    `m.values() CONTAINS` and `m.k = :p` (probed)."""
    return predicates.has_op_deep(p, ("HAS VALUE",))


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
        if leaf.op not in ("HAS ANY", "HAS ALL") or not isinstance(leaf.constant, list):
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
        boxed = [v for v in leaf.constant if isinstance(v, int) and not isinstance(v, bool) and _int32(v)]
        if not boxed:
            continue
        if served:
            if _mixed_boxes(leaf.constant):
                return "O10"
            continue
        return "O7"
    return None


def map_value_index_known(schema: Schema, table: str, p, data_model: str, edge: bool = False, servable: bool = True) -> "str | None":
    """O5's map face: `m CONTAINSVALUE :p` through a `BY VALUE` map index of a hash
    or a DICTIONARY kind is a 501 `Cannot evaluate ... on index` for every constant
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
    so a SIBLING's same-named field of another type meets the query's constant. Probed:
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


works_around("O14")


def insub_known(schema: Schema, instance: Instance, table: str, p, data_model: str, outer_served: bool = True) -> "str | None":
    """The findings of a tree's SEMI-JOIN leaves (`f IN (SELECT g FROM t2 [WHERE
    q])`, the select IR's `INSUB`): O39 for a CONTAINSVALUE in `q` (a subquery
    consumed as a value, any position); `q`'s own findings as the inner query
    filters (index-served over `t2`); and where the OUTER query may be index-served,
    O14 (a null among the answered values through an index on `f`: the null-keyed
    records come back). O10 / O21 on the outer lookup are the IN leaf's, judged by
    `o10_predicate` / `o21_predicate`."""
    for leaf in predicates.subqueries(p):
        t2, _g, q, _scan = leaf.sub
        if q is not None:
            if o39_predicate(q):
                return "O39"
            inner = node_case_known(t2, q, schema, instance).get(data_model)
            if inner:
                return inner
        if outer_served and _plain(leaf) and any(v is None for v in leaf.constant) and schema.has_index(table, leaf.field, data_model):
            return "O14"
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
        elif o7_predicate(schema, table, p):
            known_bugs[data_model] = "O7"
        elif o10_predicate(schema, table, p, data_model):
            known_bugs[data_model] = "O10"
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
