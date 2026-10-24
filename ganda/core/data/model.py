"""Abstract, storage-agnostic data model

An `Instance` is a small labelled property graph: typed node classes and typed edge
(relationship) classes connecting them, plus concrete row/edge data.

Nothing here is engine vocabulary. Engine options -- `IndexSpec.kind` and `options`,
`NodeTypeSpec.options`, `Schema.id_index`, the `WriteForm` fields -- are values this
module carries without reading: a provider's `physical_design` draws them and its
mapping reads them. A schema knows the provider it
was generated for (`Schema.engine`), which is how the layers below reach the engine's
semantics, concrete syntax and known bugs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class FieldType(Enum):
    """A column's abstract type. Each engine declares it in its own vocabulary
    (its `dialect`); the values are the model's own names, stored in bundles and
    predicates."""

    INT = "int"  # a 64-bit signed integer
    FLOAT = "float"  # an IEEE-754 double
    STRING = "string"
    BOOL = "bool"
    LIST = "list"  # a list of INT values
    # the narrow numeric types
    INT32 = "int32"
    FLOAT32 = "float32"
    DECIMAL = "decimal"
    # ISO 9075
    DATETIME = "datetime"
    # ISO 9075
    DATE = "date"
    # STRING keys from `MAP_KEYS` to INT values
    MAP = "map"
    BINARY = "binary"

INT_TYPES = (FieldType.INT, FieldType.INT32)
FLOAT_TYPES = (FieldType.FLOAT, FieldType.FLOAT32, FieldType.DECIMAL)
NUMERIC_TYPES = INT_TYPES + FLOAT_TYPES
DATE_TYPES = (FieldType.DATETIME, FieldType.DATE)
COLLECTION_TYPES = (FieldType.LIST, FieldType.MAP)
# the types with a total order (each engine's is its semantics' to state); BOOL and the
# collections are equality-only
ORDERED_TYPES = NUMERIC_TYPES + (FieldType.STRING,) + DATE_TYPES
# the types a scalar predicate / projection / GROUP BY key may name (BINARY is not one:
# it is projected, never compared or grouped)
SCALAR_TYPES = ORDERED_TYPES + (FieldType.BOOL,)
BLOB_TYPES = (FieldType.BINARY,)
# the types whose values the large-row axis grows to the engine's limits
GROWABLE_TYPES = (FieldType.STRING, FieldType.BINARY, FieldType.LIST)
MAP_KEYS = ("k1", "k2", "k3", "k4")


def options(**kw) -> tuple:
    """Engine options as hashable `(name, value)` pairs, in name order; a None value
    is left out."""
    return tuple(sorted((k, v) for k, v in kw.items() if v is not None))


class _HasOptions:
    def option(self, name: str, default: Any = None) -> Any:
        return dict(self.options).get(name, default)


@dataclass(frozen=True)
class FieldSpec:
    name: str  # e.g. "f1"
    type: FieldType
    # When set, values come from a fixed pool of exactly this many distinct values 
    # (to encourage key and group by collisions)
    cardinality: "int | None" = None
    # case-insensitive collation
    case_insensitive: bool = False


@dataclass(frozen=True)
class IndexSpec(_HasOptions):
    """A secondary index on `field` (plus `extra_fields` for a composite index) of node class
    or, with `target="edge"`, edge type `table`. `data_model` names the databases that
    get it ("both" = all). `kind` and `options` are the engine's; `ignore_nulls` says
    whether a record with a null key gets an entry."""

    table: str
    field: str
    data_model: str = "both"
    kind: "str | None" = None
    extra_fields: tuple = ()
    ignore_nulls: bool = True
    target: str = "node"
    options: tuple = ()

    @property
    def all_fields(self) -> tuple:
        return (self.field,) + tuple(self.extra_fields)

    def applies_to(self, data_model: str) -> bool:
        if self.target == "edge" and data_model == "doc":
            return False
        return self.data_model == "both" or self.data_model == data_model

    def name(self) -> str:
        return f"ix_{self.table}_{'_'.join(self.all_fields)}" + "".join(f"_{k}{str(v).lower()}" for k, v in self.options)


@dataclass(frozen=True)
class NodeTypeSpec(_HasOptions):
    name: str  # class name -- synthetic, e.g. "n1"
    fields: tuple
    options: tuple = ()  # the engine's storage options for the class


@dataclass(frozen=True)
class EdgeTypeSpec:
    name: str  # edge class / junction table / embedded link list, e.g. "e1"
    from_type: str
    to_type: str
    fields: tuple  # attributes attached to the relationship itself


DATA_MODELS = ("doc", "graph", "rel")


@dataclass(frozen=True)
class Schema:
    node_types: tuple
    edge_types: tuple
    indexes: tuple = ()
    # data model -> the kind of index on `id` (an engine option; "none": no index); a
    # missing data model means the engine's default.
    id_index: dict = field(default_factory=dict)
    # The inheritance axis: an ABSTRACT base class every node class extends, declaring
    # `id` and `base.fields` (`b1`, ...), so a query on the base is polymorphic over
    # every node class and a predicate on a base field may be served by an index on
    # the base (which covers every subclass) or on one subclass. None: every node class
    # stands alone.
    base: "NodeTypeSpec | None" = None
    # the provider this schema was generated for (`ganda.core.engine.provider.Provider`):
    # where the core finds the engine's semantics, concrete syntax and known bugs
    engine: Any = field(default=None, compare=False, repr=False)

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

    def poly_tables(self, table: str) -> set:
        """The classes whose records a query on `table` may meet through inheritance:
        the table itself, its base class, and for the base class every subclass."""
        tables = {table}
        if self.base is not None:
            tables |= {nt.name for nt in self.node_types} if self.is_base(table) else {self.base.name}
        return tables

    def edge_types_from(self, node_type: str) -> list:
        return [et for et in self.edge_types if et.from_type == node_type]

    def edge_types_to(self, node_type: str) -> list:
        return [et for et in self.edge_types if et.to_type == node_type]

    def id_index_kind(self, data_model: str) -> "str | None":
        """None: the engine's default."""
        return self.id_index.get(data_model)

    def node_indexes(self, table: str, field_name: str, data_model: str) -> list:
        """Every index on `data_model` whose LEAD field can serve a predicate on `field` of
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
        return [ix for ix in self.indexes if ix.target == "node" and ix.table in tables and ix.field == field_name and ix.applies_to(data_model)]

    def has_index(self, table: str, field_name: str, data_model: str) -> bool:
        if field_name == "id":
            return self.id_index_kind(data_model) != "none"
        return bool(self.node_indexes(table, field_name, data_model))


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
        """The edge's logical id, stored as `eid` on every data model."""
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
    its final `EdgeRow.values` -- the graph data model's edge, the junction row, the doc
    data model's link list -- so an edge index is maintained through an update, not only a
    create."""

    edge: EdgeRow
    initial_values: dict


@dataclass(frozen=True)
class NodeDelete:
    """The node was written (with its incident edges) and then deleted; neither it nor
    its incident edges are in `Instance.nodes`/`Instance.edges`. The cascade differs per
    representation: the vertex delete cascades natively on the graph data model, the doc data model
    rewrites the link lists that pointed at the node, the relational data model removes the
    junction rows."""

    node: NodeRow
    incident_edges: tuple


@dataclass(frozen=True)
class WriteForm:
    """How one side's setup script renders its steps: every combination reaches a
    different engine write path while the final state stays the same. The fields'
    vocabulary is the engine's (its `physical_design.draw_write_form`)."""

    insert_via: str = "set"
    update_form: str = "set"
    edge_from: str = "ref"
    transaction: str = "none"
    cancelled_junk: bool = False
    edge_update_form: str = "set"
    rebuild_indexes: bool = False
    # the `statements_per_request` axis: the data steps travel in requests of at most
    # this many statements through the transaction door's own bulk form (0: one request
    # per step, or whatever the door folds on its own)
    chunk_size: int = 0



PLAIN_WRITE_FORM = WriteForm()


@dataclass
class Instance:
    schema: Schema
    nodes: list
    edges: list
    churn: list = field(default_factory=list)
    # data model -> how its setup writes (a missing data model writes plainly)
    write_forms: dict = field(default_factory=dict)

    # The lookups below are answered from indexes built lazily over `nodes` and
    # `edges` and rebuilt whenever either list is another object or another length
    # (`dataclasses.replace` gives a new Instance its own), so a traversal over a
    # large instance costs its paths rather than a scan of every edge per hop. Every
    # answer is a fresh list in instance order, as the scans gave.

    def __post_init__(self):
        self._node_index = None
        self._edge_index = None

    def _nodes_indexed(self) -> tuple:
        if self._node_index is None or self._node_index[0] is not self.nodes or self._node_index[1] != len(self.nodes):
            by_id: dict = {}
            by_type: dict = {}
            for n in self.nodes:
                by_id.setdefault(n.record_id, n)
                by_type.setdefault(n.type_name, []).append(n)
            self._node_index = (self.nodes, len(self.nodes), by_id, by_type)
        return self._node_index

    def _edges_indexed(self) -> tuple:
        if self._edge_index is None or self._edge_index[0] is not self.edges or self._edge_index[1] != len(self.edges):
            by_type: dict = {}
            out: dict = {}
            inc: dict = {}
            for e in self.edges:
                by_type.setdefault(e.type_name, []).append(e)
                out.setdefault((e.type_name, e.from_record), []).append(e)
                inc.setdefault((e.type_name, e.to_record), []).append(e)
            self._edge_index = (self.edges, len(self.edges), by_type, out, inc)
        return self._edge_index

    def write_form(self, data_model: str) -> WriteForm:
        return self.write_forms.get(data_model, PLAIN_WRITE_FORM)

    def nodes_of(self, type_name: str) -> list:
        if self.schema.is_base(type_name):
            return list(self.nodes)
        return list(self._nodes_indexed()[3].get(type_name, ()))

    def edges_of(self, type_name: str) -> list:
        return list(self._edges_indexed()[2].get(type_name, ()))

    def node_by_id(self, record_id: str) -> "NodeRow | None":
        return self._nodes_indexed()[2].get(record_id)

    def outgoing(self, edge_type: str, from_record: str) -> list:
        """Ground-truth adjacency: every edge of `edge_type` leaving `from_record`, in
        instance order (parallel edges repeat)."""
        return list(self._edges_indexed()[3].get((edge_type, from_record), ()))

    def incoming(self, edge_type: str, to_record: str) -> list:
        return list(self._edges_indexed()[4].get((edge_type, to_record), ()))


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
    collapse `1.0` onto `1`: every column has one declared type on every data model, so an
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
