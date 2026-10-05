"""Abstract, storage-agnostic data model -- the pivot every other layer hangs off.

An `Instance` is a small labelled property graph: typed node classes and typed edge
(relationship) classes connecting them, plus concrete row/edge data, materialized three
equivalent ways (`SIDES`: document, graph, relational).

Third port of the Ganda model (SurrealDB, ArangoDB, now OrientDB); same class names,
same churn ops, same `IndexSpec.side` idea, so the three fuzzers read side by side.
Nothing here is engine vocabulary except the VALUES of a few physical-design knobs --
`IndexSpec.kind` / `ignore_nulls` / `by`, `Schema.id_index`, `NodeTypeSpec.clusters`,
the `WriteForm` fields -- which this module carries without interpreting; their
vocabulary and the engine facts behind every type are `ganda.orientdb.orient`'s.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class FieldType(Enum):
    """A column's abstract type. Each engine declares it in its own vocabulary
    (`orient.ORIENT_TYPES`); the values are the model's own names, stored in bundles,
    predicates and card corpora."""

    INT = "int"  # a 64-bit signed integer
    FLOAT = "float"  # an IEEE-754 double
    STRING = "string"
    BOOL = "bool"
    LIST = "list"  # a list of INT values
    # the narrow numeric types (the `narrow_type_probability` axis), each value exact
    # in JSON: a 32-bit signed integer; a float32 whose shortest decimal is its exact
    # value (a multiple of 1/8 below 2^10, or 2^24); an exact decimal with at most
    # three decimal places
    INT32 = "int32"
    FLOAT32 = "float32"
    DECIMAL = "decimal"
    # a second-precision UTC timestamp / a calendar date, valued as its canonical
    # `yyyy-MM-dd HH:mm:ss` string (a DATE at midnight): lexicographic order on that
    # form IS chronological order
    DATETIME = "datetime"
    DATE = "date"
    # string keys from `MAP_KEYS` to INT values
    MAP = "map"

INT_TYPES = (FieldType.INT, FieldType.INT32)
FLOAT_TYPES = (FieldType.FLOAT, FieldType.FLOAT32, FieldType.DECIMAL)
NUMERIC_TYPES = INT_TYPES + FLOAT_TYPES
DATE_TYPES = (FieldType.DATETIME, FieldType.DATE)
COLLECTION_TYPES = (FieldType.LIST, FieldType.MAP)
# the types with a total order (each engine's is `orient`'s to state); BOOL and the
# collections are equality-only
ORDERED_TYPES = NUMERIC_TYPES + (FieldType.STRING,) + DATE_TYPES
# the types a scalar predicate / projection / GROUP BY key may name
SCALAR_TYPES = ORDERED_TYPES + (FieldType.BOOL,)
MAP_KEYS = ("k1", "k2", "k3", "k4")


@dataclass(frozen=True)
class FieldSpec:
    name: str  # e.g. "f1"
    type: FieldType
    # When set, values come from a fixed pool of exactly this many distinct values --
    # what makes GROUP BY keys and predicate collisions happen on purpose.
    cardinality: "int | None" = None
    # "ci": a case-insensitive collation (the `collate_probability` axis, STRING
    # fields); which operators honour it is the engine's (`orient`)
    collate: "str | None" = None

    @property
    def ci(self) -> bool:
        return self.collate == "ci"


@dataclass(frozen=True)
class IndexSpec:
    """A secondary index on node field `field` (plus `extra_fields`, a composite) of
    class `table` (or, with `target == "edge"`, on attribute `field` of edge type
    `table`: the graph side's edge class and the relational side's junction table; the
    doc side has none). `side` picks which materialization defines it: "doc", "graph",
    "rel" or "both" (= every side). A one-sided index turns every identical-text case
    whose WHERE touches the field into an index-vs-scan oracle across two databases
    holding the same node data. `kind`, `ignore_nulls` (whether a record with a null
    key gets an entry) and `by` (a map index's key or value side) are engine options
    (`orient.INDEX_KINDS`)."""

    table: str
    field: str
    side: str = "both"
    kind: str = "NOTUNIQUE"
    extra_fields: tuple = ()
    ignore_nulls: bool = True
    target: str = "node"
    # a map index: `BY KEY` (serves CONTAINSKEY) or `BY VALUE` (serves CONTAINSVALUE)
    by: "str | None" = None

    @property
    def all_fields(self) -> tuple:
        return (self.field,) + tuple(self.extra_fields)

    def applies_to(self, side: str) -> bool:
        if self.target == "edge" and side == "doc":
            return False
        return self.side == "both" or self.side == side

    def name(self) -> str:
        return f"ix_{self.table}_{'_'.join(self.all_fields)}" + (f"_by{self.by.lower()}" if self.by else "")


@dataclass(frozen=True)
class NodeTypeSpec:
    name: str  # class name -- synthetic, e.g. "n1"
    fields: tuple
    # an engine option: the number of physical partitions the class's records are
    # spread over (the `clusters_probability` axis, `orient`)
    clusters: int = 1


@dataclass(frozen=True)
class EdgeTypeSpec:
    name: str  # edge class / junction table / embedded link list, e.g. "e1"
    from_type: str
    to_type: str
    fields: tuple  # attributes attached to the relationship itself


SIDES = ("doc", "graph", "rel")


@dataclass(frozen=True)
class Schema:
    node_types: tuple
    edge_types: tuple
    indexes: tuple = ()
    # side -> the kind of index on `id` (an engine option, `orient.ID_INDEX_KINDS`;
    # "none": no index); a missing side means "UNIQUE".
    id_index: dict = field(default_factory=dict)
    # The inheritance axis: an ABSTRACT base class every node class extends, declaring
    # `id` and `base.fields` (`b1`, ...), so a query on the base is polymorphic over
    # every node class and a predicate on a base field may be served by an index on
    # the base (which covers every subclass) or on one subclass. None: every node class
    # stands alone.
    base: "NodeTypeSpec | None" = None

    def node_type(self, name: str) -> NodeTypeSpec:
        for nt in self.node_types:
            if nt.name == name:
                return nt
        if self.base is not None and self.base.name == name:
            return self.base
        raise KeyError(name)

    def is_base(self, table: str) -> bool:
        return self.base is not None and self.base.name == table

    def query_tables(self) -> list:
        """The node tables a query may name: every node class, and the base class
        when it has fields of its own."""
        out = list(self.node_types)
        if self.base is not None and self.base.fields:
            out.append(self.base)
        return out

    def edge_type(self, name: str) -> EdgeTypeSpec:
        for et in self.edge_types:
            if et.name == name:
                return et
        raise KeyError(name)

    def field(self, table: str, field_name: str, edge: bool = False) -> FieldSpec:
        fields = self.edge_type(table).fields if edge else self.query_fields(table)
        for f in fields:
            if f.name == field_name:
                return f
        raise KeyError(field_name)

    def query_fields(self, table: str) -> tuple:
        """The fields a query on `table` may name (a subclass sees its own and the
        base class's, the base class its own)."""
        if self.is_base(table):
            return self.base.fields
        own = self.node_type(table).fields
        return own + self.base.fields if self.base is not None else own

    def edge_types_from(self, node_type: str) -> list:
        return [et for et in self.edge_types if et.from_type == node_type]

    def edge_types_to(self, node_type: str) -> list:
        return [et for et in self.edge_types if et.to_type == node_type]

    def id_index_kind(self, side: str) -> str:
        return self.id_index.get(side, "UNIQUE")

    def node_indexes(self, table: str, field_name: str, side: str) -> list:
        """Every index on `side` whose LEAD field can serve a predicate on `field` of
        a query over `table` (a composite index serves its first field): the indexes on
        the table itself, those on the base class (which cover every subclass), and for
        a query on the base class those on any subclass (a query on the base is treated
        as index-served as soon as one does)."""
        tables = {table}
        if self.base is not None:
            if self.is_base(table):
                tables |= {nt.name for nt in self.node_types}
            else:
                tables.add(self.base.name)
        return [ix for ix in self.indexes if ix.target == "node" and ix.table in tables and ix.field == field_name and ix.applies_to(side)]

    def has_index(self, table: str, field_name: str, side: str) -> bool:
        if field_name == "id":
            return self.id_index_kind(side) != "none"
        return bool(self.node_indexes(table, field_name, side))


@dataclass(frozen=True)
class NodeRow:
    type_name: str
    local_id: int
    values: dict  # field name -> JSON value; an omitted field is simply absent
    custom_key: "str | None" = None

    @property
    def key(self) -> str:
        return self.custom_key if self.custom_key is not None else f"r{self.local_id}"

    @property
    def record_id(self) -> str:
        return f"{self.type_name}/{self.key}"


@dataclass(frozen=True)
class EdgeRow:
    type_name: str
    local_id: int
    from_record: str  # NodeRow.record_id
    to_record: str
    values: dict

    @property
    def key(self) -> str:
        return f"e{self.local_id}"

    @property
    def record_id(self) -> str:
        """The edge's logical id, stored as `eid` on every side."""
        return f"{self.type_name}/{self.key}"


@dataclass(frozen=True)
class NodeUpdate:
    """The node was first written with `initial_values` and then updated to its final
    `NodeRow.values` -- so the write path (index maintenance included) is exercised
    while the final state still equals the Instance."""

    node: NodeRow
    initial_values: dict


@dataclass(frozen=True)
class EdgeDelete:
    """The edge was written and then deleted; it is NOT in `Instance.edges`."""

    edge: EdgeRow


@dataclass(frozen=True)
class EdgeUpdate:
    """The relationship was first written with `initial_values` and then updated to
    its final `EdgeRow.values` -- the graph side's edge, the junction row, the doc
    side's link list -- so an edge index is maintained through an update, not only a
    create."""

    edge: EdgeRow
    initial_values: dict


@dataclass(frozen=True)
class NodeDelete:
    """The node was written (with its incident edges) and then deleted; neither it nor
    its incident edges are in `Instance.nodes`/`Instance.edges`. The cascade differs per
    representation: the vertex delete cascades natively on the graph side, the doc side
    rewrites the link lists that pointed at the node, the relational side removes the
    junction rows."""

    node: NodeRow
    incident_edges: tuple


@dataclass(frozen=True)
class WriteForm:
    """How one side's setup script spells its steps: every combination reaches a
    different engine write path while the final state stays the same. The fields'
    vocabulary is the engine's (`orient`'s write forms)."""

    insert_via: str = "set"
    update_form: str = "set"
    edge_from: str = "ref"
    transaction: str = "none"
    cancelled_junk: bool = False
    edge_update_form: str = "set"
    rebuild_indexes: bool = False


PLAIN_WRITE_FORM = WriteForm()


@dataclass
class Instance:
    schema: Schema
    nodes: list
    edges: list
    churn: list = field(default_factory=list)
    doc_write_form: WriteForm = PLAIN_WRITE_FORM
    graph_write_form: WriteForm = PLAIN_WRITE_FORM
    rel_write_form: WriteForm = PLAIN_WRITE_FORM

    def write_form(self, side: str) -> WriteForm:
        return {"doc": self.doc_write_form, "graph": self.graph_write_form, "rel": self.rel_write_form}[side]

    def nodes_of(self, type_name: str) -> list:
        if self.schema.is_base(type_name):
            return list(self.nodes)
        return [n for n in self.nodes if n.type_name == type_name]

    def edges_of(self, type_name: str) -> list:
        return [e for e in self.edges if e.type_name == type_name]

    def node_by_id(self, record_id: str) -> "NodeRow | None":
        for n in self.nodes:
            if n.record_id == record_id:
                return n
        return None

    def outgoing(self, edge_type: str, from_record: str) -> list:
        """Ground-truth adjacency: every edge of `edge_type` leaving `from_record`, in
        instance order (parallel edges repeat)."""
        return [e for e in self.edges if e.type_name == edge_type and e.from_record == from_record]

    def incoming(self, edge_type: str, to_record: str) -> list:
        return [e for e in self.edges if e.type_name == edge_type and e.to_record == to_record]

    def both(self, edge_type: str, record: str) -> list:
        """Both directions: the outgoing edges followed by the incoming ones -- a
        self-loop is in both lists and is visited twice (a vertex with one self-loop and
        two parallel edges has a multiset of five)."""
        return self.outgoing(edge_type, record) + self.incoming(edge_type, record)

    def adjacency(self, edge_type: str) -> dict:
        adj: dict = {}
        for e in self.edges:
            if e.type_name == edge_type:
                adj.setdefault(e.from_record, []).append(e.to_record)
        return adj


def hashable_value(v: Any):
    """A hashable stand-in for a JSON value, for dict keys and Counters. It does NOT
    collapse `1.0` onto `1`: every column has one declared type on every side, so an
    integer and a float answer for the same expression is a divergence, not a wire
    artifact."""
    if isinstance(v, bool):
        return ("b", v)
    if isinstance(v, float):
        return ("f", v, 1 if str(v)[0] != "-" else -1)  # keeps -0.0 apart from 0.0
    if isinstance(v, dict):
        return ("__obj__",) + tuple(sorted((k, hashable_value(x)) for k, x in v.items()))
    if isinstance(v, list):
        return ("__arr__",) + tuple(hashable_value(x) for x in v)
    return v
