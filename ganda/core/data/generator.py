"""Random, seeded generation of schemas and data instances.

Identifiers are synthetic (`n1`, `e1`, `f1`, `r0`) 

Customizations that can be switched off, e.g. node deletion, case-insensitive collation, list indexes, storage options
draw from *tail* streams keyed by their own name, so at probability 0 they reproduce exactly what 
the generator produced without them.

The engine's limits (`Provider.limits`, `ganda.core.data.limits`) bound what the large-data
axes produce: a grown value stays under the type's bound and the record budget, an
indexed one under the index key bound, a hub under the fan-out bound.
"""
from __future__ import annotations

import base64
import dataclasses
import datetime as _dt
import math
import random

from ganda.core.data import scale as _scale
from ganda.core.constants import I64_MAX, I64_MIN
from ganda.core.data.limits import StorageLimits
from ganda.core.rng import QueryRng, stream as _stream
from dataclasses import dataclass
from typing import ClassVar

from ganda.core.data.model import (
    BLOB_TYPES,
    GROWABLE_TYPES,
    MAP_KEYS,
    NUMERIC_TYPES,
    EdgeDelete,
    EdgeRow,
    EdgeTypeSpec,
    EdgeUpdate,
    FieldSpec,
    FieldType,
    IndexSpec,
    Instance,
    NodeDelete,
    NodeRow,
    NodeTypeSpec,
    NodeUpdate,
    Schema,
    WriteForm,
)
from ganda.core.engine.setup_plan import initial_edge_values, initial_edges


_FIELD_TYPE_WEIGHTS = {
    FieldType.INT: 3,
    FieldType.FLOAT: 2,
    FieldType.STRING: 3,
    FieldType.BOOL: 1,
    FieldType.LIST: 1,
}
_CARDINALITY_CLASSES = (2, 3, 5)

F64_INT_EXACT = 2**53
DBL_MAX = 1.7976931348623157e308


def _b64(raw: bytes) -> str:
    """A byte string's model value: its standard base64 text, padded."""
    return base64.b64encode(raw).decode("ascii")


def limits_of(engine) -> StorageLimits:
    """The engine's limits, or the conservative set for a provider that declares none."""
    limits = getattr(engine, "limits", None)
    return limits if limits is not None else StorageLimits.conservative()

# Adversarial per-type value pools. NaN/Infinity are excluded because JSON cannot carry
# them; strings are built with `chr()` where an editor could normalize them. A provider
# whose engine cannot store one of these unchanged gives its own table
# (`physical_design.BOUNDARY_VALUES`).
BOUNDARY_VALUES = {
    FieldType.INT: [
        0, 1, -1, 2, 7, 2**31 - 1, -(2**31), 2**31, 2**32, F64_INT_EXACT - 1, F64_INT_EXACT,
        F64_INT_EXACT + 1, 2**62, -(2**62), I64_MAX, I64_MIN, I64_MAX - 1, 123456789012345678,
    ],
    FieldType.FLOAT: [
        0.0, -0.0, 0.5, -0.5, 0.1, 0.2, 0.30000000000000004, 0.3, 1.0, -1.0, 1e-308, 5e-324,
        2.2250738585072014e-308, DBL_MAX, -DBL_MAX, 1e15 + 0.5,
        float(F64_INT_EXACT), 9007199254740993.0, 1e18, 1e300, 3.141592653589793, 100.0,
    ],
    FieldType.STRING: [
        "", " ", "a", "A", "b", "B", "z", "Z", "aa", chr(0x00E9), "e" + chr(0x0301), chr(0x0130), chr(0x0131),
        chr(0x00DF), "ss", "SS", " x", "  ", chr(0x200B), "null", "true", "false", "1", "10", "9", "-1",
        "1.5", "0", "n1/r0", "n1/r1", "e1/e0", "a b", "a  b", chr(0x1F600), chr(0x4E2D) + chr(0x6587),
        chr(0xFFFF), "SELECT", "FROM", "$current", ":p", "\"quoted\"", "back\\slash", "it's", "%", "_",
        "[]", "{}", "a\nb", "a\tb", "x" * 254, "2024-01-01T00:00:00Z", "1970-01-01", "abc", "abd", "ab",
    ],
    FieldType.BOOL: [True, False],
    FieldType.LIST: [
        [], [0], [1], [1, 1], [1, 2, 3], [3, 2, 1], [-1, 0, 1], [I64_MAX], [I64_MIN], [F64_INT_EXACT],
        [F64_INT_EXACT + 1], [0, 0, 0], [2**62, 2**62], [7, -7],
    ],
    # int32 values: the boundaries of the type, where a key conversion that wraps or
    # truncates would show
    FieldType.INT32: [0, 1, -1, 2, 7, 2**31 - 1, -(2**31), 2**31 - 2, -(2**31) + 1, 65536, -65536, 32767, -32768, 255, 256, 1000, 123456789, 2**24, 2**24 + 1],
    # float32 values whose shortest decimal is exact (multiples of 1/8 below 2^10, and
    # 2^24, the last integer a float holds exactly)
    FieldType.FLOAT32: [0.0, -0.0, 0.5, -0.5, 0.125, -0.125, 1.0, -1.0, 1023.875, -1023.875, 2.0, 100.0, 0.25, 0.75, 1024.0, 16777216.0, 3.5, 999.125],
    # decimals with at most three decimals (their shortest repr is what
    # `BigDecimal.valueOf` stores); no -0.0 (a BigDecimal has none)
    FieldType.DECIMAL: [0.0, 0.5, 0.1, 0.2, 0.3, 0.25, 1.5, -1.5, 1.0, -1.0, 100.0, 123.456, -123.456, 0.001, -0.001, 999999.999, 1e6 + 0.25, 0.125, 5.0, 3.142],
    # the database's `yyyy-MM-dd HH:mm:ss` form (UTC): the epoch, the second before
    # it, leap days, the 32-bit rollover, the last second of the format's range
    FieldType.DATETIME: [
        "1970-01-01 00:00:00", "1969-12-31 23:59:59", "1970-01-01 00:00:01", "2000-02-29 00:00:00", "2024-02-29 23:59:59",
        "2038-01-19 03:14:07", "2038-01-19 03:14:08", "1900-01-01 00:00:00", "9999-12-31 23:59:59", "2024-01-01 00:00:00",
        "2024-01-01 12:34:56", "2024-12-31 23:59:59", "1999-12-31 23:59:59", "2000-01-01 00:00:00", "2024-03-10 02:30:00",
    ],
    FieldType.DATE: [
        "1970-01-01 00:00:00", "1969-12-31 00:00:00", "2000-02-29 00:00:00", "2024-02-29 00:00:00", "1900-01-01 00:00:00",
        "9999-12-31 00:00:00", "2024-01-01 00:00:00", "2024-12-31 00:00:00", "1999-12-31 00:00:00", "2000-01-01 00:00:00",
    ],
    FieldType.MAP: [{}, {"k1": 0}, {"k1": 1, "k2": 2}, {"k2": 2**40}, {"k1": -1, "k2": 0, "k3": 1}, {"k4": I64_MAX}, {"k1": 1, "k1x": 1} if False else {"k3": 7, "k4": -7}, {"k1": 2**31 - 1, "k2": 2**31}],
    # byte strings as their base64 text: single bytes, runs, every byte value, the
    # three padding lengths, bytes that read as text or as a record id, invalid UTF-8,
    # a byte-order mark. No EMPTY byte string: an engine may store it as null (one
    # does, probed), which would be a divergence of the fuzzer's own making
    FieldType.BINARY: [
        _b64(b) for b in (
            b"\x00", b"\xff", b"\x00" * 8, b"\xff" * 8, b"a", b"ab", b"abc", bytes(range(16)), bytes(range(256)), b"null", b"true", b"0",
            b" ", b"\n", b"\xc3\xa9", b"\x80", b"\xff\xfe", b"\xef\xbb\xbf", b"n1/r0", b"\x00\x01\x00\x01",
        )
    ],
}

EPOCH = _dt.datetime(1970, 1, 1)


def format_datetime(t: _dt.datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S")


def random_datetime(rng: random.Random, date_only: bool = False) -> str:
    seconds = rng.randint(int(-2208988800), int(4102444799))  # 1900-01-01 .. 2099-12-31
    t = EPOCH + _dt.timedelta(seconds=seconds)
    if date_only:
        t = t.replace(hour=0, minute=0, second=0)
    return format_datetime(t)
# A value shaped like an engine's own record id is deliberately NOT in the string pool:
# an engine may convert it to a link inside an embedded document, which would make the
# doc data model's link entries type-divergent by construction.

_ARRAY_MAX_LEN = 4


def random_value(rng: random.Random, ftype: FieldType):
    if ftype is FieldType.INT or ftype is FieldType.INT32:
        return rng.randint(-1000, 1000)
    if ftype is FieldType.FLOAT or ftype is FieldType.DECIMAL:
        return round(rng.uniform(-1000.0, 1000.0), 3)
    if ftype is FieldType.FLOAT32:
        return rng.randint(-8000, 8000) / 8
    if ftype is FieldType.DATETIME:
        return random_datetime(rng)
    if ftype is FieldType.DATE:
        return random_datetime(rng, date_only=True)
    if ftype is FieldType.MAP:
        keys = rng.sample(MAP_KEYS, rng.randint(0, len(MAP_KEYS)))
        return {k: rng.randint(-100, 100) for k in keys}
    if ftype is FieldType.STRING:
        return "".join(rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(rng.randint(1, 8)))
    if ftype is FieldType.BINARY:
        return _b64(rng.randbytes(rng.randint(1, 8)))
    if ftype is FieldType.BOOL:
        return rng.random() < 0.5
    if ftype is FieldType.LIST:
        return [rng.randint(-100, 100) for _ in range(rng.randint(0, _ARRAY_MAX_LEN))]
    raise ValueError(ftype)


def boundary_value(rng: random.Random, ftype: FieldType, pool: "dict | None" = None):
    # a provider's own table may leave a type to the core's
    v = rng.choice((pool or {}).get(ftype) or BOUNDARY_VALUES[ftype])
    if isinstance(v, dict):
        return dict(v)
    return list(v) if isinstance(v, list) else v


@dataclass
class GeneratorConfig:
    # one line per field: the text of its `--flag` in `run --help` (`core/campaign/config.py`)
    HELP: ClassVar[dict] = {
        "min_node_types": "fewest node classes in a generated schema",
        "max_node_types": "most node classes in a generated schema",
        "min_edge_types": "fewest edge types in a generated schema",
        "max_edge_types": "most edge types in a generated schema",
        "min_fields_per_type": "fewest fields on a node class",
        "max_fields_per_type": "most fields on a node class",
        "min_nodes_per_type": "fewest rows in a node class",
        "max_nodes_per_type": "most rows in a node class",
        "min_edges_per_type": "fewest edges of an edge type",
        "max_edges_per_type": "most edges of an edge type",
        "null_probability": "chance a field is omitted from a row (absent: IS NULL true, IS PRESENT false)",
        "explicit_null_probability": "chance a field holds an explicit stored null (IS NULL and IS PRESENT both true)",
        "empty_table_probability": "chance a node class is generated with no rows",
        "self_loop_probability": "chance an edge between a class and itself is a self-loop",
        "parallel_edge_probability": "chance an edge repeats an existing source and target pair",
        "hub_probability": "chance an edge starts at the edge type's hub node (a high fan-out vertex)",
        "boundary_value_probability": "chance a stored value is a type's boundary value (min, max, empty, zero, ...)",
        "value_reuse_probability": "chance a stored value repeats one already in the column (duplicates)",
        "cardinality_control_probability": "chance a field is limited to 2, 3 or 5 distinct values",
        "index_probability": "chance a scalar node field is indexed",
        "composite_index_probability": "chance a class gets an index over two fields",
        "edge_index_probability": "chance an edge type gets an index on one edge attribute",
        "demote_colliding_indexes": "give a one-record-per-key index the provider's fallback kind when the values collide",
        "churn_update_probability": "chance a stored row is updated after setup (churn)",
        "churn_delete_probability": "chance a stored edge is deleted after setup (churn)",
        "churn_node_delete_probability": "chance a stored node, and its edges, is deleted after setup (churn)",
        "custom_key_probability": "chance a class uses custom record keys (strings, some non-ASCII) instead of generated ids",
        "write_form_probability": "chance setup writes go through the engine's alternative write form",
        "inheritance_probability": "chance of an abstract base class every node class extends",
        "narrow_type_probability": "chance an INT or FLOAT field becomes a narrower numeric column (INTEGER, FLOAT, DECIMAL)",
        "date_map_probability": "chance an INT field becomes a DATETIME or DATE column, or a LIST field a map column",
        "collate_probability": "chance a STRING field is declared case-insensitive",
        "list_index_probability": "chance a LIST or MAP field gets an index over its elements, keys or values",
        "churn_edge_update_probability": "chance an edge is written with initial attributes and then updated",
        "traversal_cases": "traversal questions asked per generated instance",
        "select_cases": "single-class (select) questions asked per generated instance",
        "path_cases": "shortest-path questions asked per generated instance",
        "write_cases": "predicate-targeted writes issued after the reads, each checked",
        "scale": "scale profile: default, or large for the storage-engine stress mode",
        "large_row_probability": "chance a row grows its strings, binaries and lists toward the engine's limits",
        "binary_probability": "chance a STRING field becomes a BINARY column (a stored blob, never compared)",
        "statements_per_request": "statements per setup request (0: the engine's own bulk form)",
    }

    min_node_types: int = 2
    max_node_types: int = 10
    min_edge_types: int = 2
    max_edge_types: int = 20
    min_fields_per_type: int = 1
    max_fields_per_type: int = 4
    min_nodes_per_type: int = 10
    max_nodes_per_type: int = 40
    min_edges_per_type: int = 15
    max_edges_per_type: int = 60
    # A field is omitted from a row (absent; `IS NULL` true, `IS PRESENT` false).
    null_probability: float = 0.1
    # An explicit stored `null` (`IS NULL` true, `IS PRESENT` true).
    explicit_null_probability: float = 0.05
    empty_table_probability: float = 0.1
    # Topology shaping.
    self_loop_probability: float = 0.15
    parallel_edge_probability: float = 0.15
    hub_probability: float = 0.25
    # Value shaping.
    boundary_value_probability: float = 0.2
    value_reuse_probability: float = 0.2
    cardinality_control_probability: float = 0.3
    # Indexes: per scalar node field; data model drawn from doc/graph/rel/both; kind drawn
    # by the provider (`physical_design.draw_index_kind`).
    index_probability: float = 0.35
    composite_index_probability: float = 0.15
    # Per edge type: an index on one edge attribute (graph E class / junction class).
    edge_index_probability: float = 0.2
    # An index that holds one record per key, over generated values that collide, falls
    # back to the kind its provider names (`physical_design.demoted_kind`). Draws
    # nothing.
    demote_colliding_indexes: bool = True
    # Churn.
    churn_update_probability: float = 0.15
    churn_delete_probability: float = 0.1
    churn_node_delete_probability: float = 0.08
    # Tail-stream axes (0.0 reproduces prior generation exactly).
    custom_key_probability: float = 0.0
    write_form_probability: float = 0.5
    # The `id` index kind per data model (`ID_INDEX_KINDS`); at 0 every data model gets UNIQUE.
    # An abstract base class every node class extends, with 1-3 fields of its own
    # (`b1`, ...) that every node carries, and indexes on those fields placed on the
    # base or on one subclass (`Schema.base`). At 0 no base class exists.
    inheritance_probability: float = 0.0
    # Per node field: an INT field becomes an INTEGER, a FLOAT field a FLOAT or a
    # DECIMAL column (`FieldType.INT32` / `FLOAT32` / `DECIMAL`). The row streams are
    # unchanged, so only the retyped column's values move.
    narrow_type_probability: float = 0.0
    # Per node field: an INT field becomes a DATETIME or a DATE column, a LIST field
    # a MAP; tail streams too.
    date_map_probability: float = 0.0
    # Per STRING node field: a case-insensitive collation, and every stored value of the
    # field is case-flipped with probability 0.5 so case variants collide.
    collate_probability: float = 0.0
    # Per LIST or MAP node field: a non-unique index over its elements; the provider
    # draws the index's options (`physical_design.draw_index_options`).
    list_index_probability: float = 0.0
    # Per edge: the relationship is written with initial attribute values and then
    # updated to its final ones (`EdgeUpdate`), through the data model's edge update form.
    churn_edge_update_probability: float = 0.0
    # The case supply per instance (draws nothing from the instance's streams):
    # grammar-sampled traversal questions (a prefix-stable sample: the first 10 of 30
    # are the 10). Here so a bundle's `repro.py` and a mutation report carry them.
    traversal_cases: int = 30
    # grammar-sampled single-class questions (`ir_select`, its own sub-stream: adding
    # them moves no other case), prefix-stable like `traversal_cases`
    select_cases: int = 180
    # the shortest-path questions (`ir_path`, `queries/path_ast.py`): its own
    # sub-stream after the select supply, 0 reproduces a run from before it
    path_cases: int = 3
    # the write phase (`ganda.core.query.generators.write_phase`, `ir_write`): this many predicate-targeted
    # writes after every read case, each checked (answers, state, read-back) and
    # applied to the Instance; its own sub-stream, 0 reproduces a run from before it
    write_cases: int = 8
    # The scale profile (`ganda.core.data.scale`): "default", or "large" for the
    # storage-engine stress mode, which sets the sizes above and the three axes below
    # wherever their flags were left at these defaults, and runs the campaign without
    # the reference leg.
    scale: str = "default"
    # Per node row, on a tail stream: the row is a LARGE row, whose present STRING /
    # BINARY / LIST values (the uncontrolled ones) grow to the engine's limits
    # (`Provider.limits`) within a quarter of its record budget, an indexed field
    # within its index key bound. Edge attributes never grow: a data model may fold a
    # record's relationships into one statement.
    large_row_probability: float = 0.0
    # Per STRING node field, on a tail stream: the column becomes a BINARY column
    # (`FieldType.BINARY`, values as base64 text); an index drawn on it is dropped.
    # Nothing on an engine without a binary type.
    binary_probability: float = 0.0
    # The data steps of every setup travel in requests of at most this many
    # statements through the transaction door's bulk form (`WriteForm.chunk_size`);
    # 0 sends them as the door does on its own. Draws nothing.
    statements_per_request: int = 0

    def with_(self, **kw) -> "GeneratorConfig":
        return dataclasses.replace(self, **kw)


def _random_field_specs(rng: random.Random, count: int, cfg: GeneratorConfig, prefix: str = "f") -> tuple:
    specs = []
    for i in range(count):
        (ftype,) = rng.choices(list(_FIELD_TYPE_WEIGHTS), weights=list(_FIELD_TYPE_WEIGHTS.values()))
        cardinality = rng.choice(_CARDINALITY_CLASSES) if rng.random() < cfg.cardinality_control_probability else None
        specs.append(FieldSpec(name=f"{prefix}{i + 1}", type=ftype, cardinality=cardinality))
    return tuple(specs)


BASE_CLASS = "N0"


def _base_class(seed: "int | None", cfg: GeneratorConfig, node_names: list, engine) -> "tuple[NodeTypeSpec | None, list]":
    """The inheritance axis, on its own tail stream: the base class's fields and the
    indexes on them (on the base, or on one subclass)."""
    if seed is None or cfg.inheritance_probability <= 0:
        return None, []
    r = _stream(seed, "inheritance")
    if r.random() >= cfg.inheritance_probability:
        return None, []
    base = NodeTypeSpec(name=BASE_CLASS, fields=_random_field_specs(r, r.randint(1, 3), cfg, prefix="b"))
    indexes = []
    for f in base.fields:
        if f.type is FieldType.LIST or r.random() >= cfg.index_probability:
            continue
        table = BASE_CLASS if r.random() < 0.5 else r.choice(node_names)
        indexes.append(IndexSpec(table=table, field=f.name, data_model=r.choice(engine.data_models + ("both",)), kind=engine.physical_design.draw_index_kind(r), ignore_nulls=r.random() < 0.5))
    return base, indexes


def _draw_indexes(rng: random.Random, cfg: GeneratorConfig, node_types: tuple, edge_types: tuple, engine) -> list:
    pd = engine.physical_design
    data_models = engine.data_models + ("both",)
    indexes = []
    for nt in node_types:
        scalar = [f for f in nt.fields if f.type is not FieldType.LIST]
        for f in scalar:
            if rng.random() < cfg.index_probability:
                indexes.append(IndexSpec(table=nt.name, field=f.name, data_model=rng.choice(data_models), kind=pd.draw_index_kind(rng), ignore_nulls=rng.random() < 0.5))
        if len(scalar) >= 2 and rng.random() < cfg.composite_index_probability:
            a, b = rng.sample(scalar, 2)
            indexes.append(IndexSpec(table=nt.name, field=a.name, extra_fields=(b.name,), data_model=rng.choice(data_models), kind=rng.choice(pd.NON_UNIQUE_KINDS)))
    for et in edge_types:
        scalar = [f for f in et.fields if f.type is not FieldType.LIST]
        if scalar and rng.random() < cfg.edge_index_probability:
            indexes.append(IndexSpec(table=et.name, field=rng.choice(scalar).name, data_model=rng.choice(pd.EDGE_INDEX_DATA_MODELS), kind=rng.choice(pd.NON_UNIQUE_KINDS), target="edge"))
    return indexes


def _draw_node_types(rng: random.Random, cfg: GeneratorConfig) -> tuple:
    count = rng.randint(cfg.min_node_types, cfg.max_node_types)
    return tuple(
        NodeTypeSpec(name=f"n{i + 1}", fields=_random_field_specs(rng, rng.randint(cfg.min_fields_per_type, cfg.max_fields_per_type), cfg))
        for i in range(count)
    )


def _draw_edge_types(rng: random.Random, cfg: GeneratorConfig, node_names: list) -> tuple:
    count = rng.randint(cfg.min_edge_types, cfg.max_edge_types)
    return tuple(
        EdgeTypeSpec(
            name=f"e{i + 1}",
            from_type=rng.choice(node_names),
            to_type=rng.choice(node_names),
            fields=_random_field_specs(rng, rng.randint(0, cfg.max_fields_per_type), cfg),
        )
        for i in range(count)
    )


def generate_schema(rng: random.Random, cfg: GeneratorConfig, seed: "int | None" = None, *, engine) -> Schema:
    pd = engine.physical_design

    node_types = _draw_node_types(rng, cfg)
    node_names = [nt.name for nt in node_types]
    edge_types = _draw_edge_types(rng, cfg, node_names)
    indexes = _draw_indexes(rng, cfg, node_types, edge_types, engine)

    id_index = pd.draw_id_indexes(seed, cfg)
    base, base_indexes = _base_class(seed, cfg, node_names, engine)
    node_types, base = _tail_field_axes(seed, cfg, node_types, base, limits_of(engine))
    node_types = pd.draw_storage_options(seed, cfg, node_types)

    indexes = _without_blob_indexes(indexes + base_indexes, node_types, base)
    indexes += _list_indexes(seed, cfg, node_types, base, engine)
    return Schema(
        engine=engine,
        node_types=node_types,
        edge_types=edge_types,
        indexes=tuple(_dedupe_indexes(indexes, engine.data_models)),
        id_index=id_index,
        base=base,
    )


def _retype(seed: "int | None", cfg: GeneratorConfig, table: str, f: FieldSpec, limits: StorageLimits) -> FieldSpec:
    """The narrow-type, date / map, binary and collation tail axes for one node field."""
    if seed is None:
        return f
    if cfg.narrow_type_probability > 0 and f.type in (FieldType.INT, FieldType.FLOAT):
        r = _stream(seed, "types", table, f.name)
        if r.random() < cfg.narrow_type_probability:
            new = FieldType.INT32 if f.type is FieldType.INT else r.choice((FieldType.FLOAT32, FieldType.DECIMAL))
            f = dataclasses.replace(f, type=new)
    if cfg.date_map_probability > 0 and f.type in (FieldType.INT, FieldType.LIST):
        r = _stream(seed, "datemap", table, f.name)
        if r.random() < cfg.date_map_probability:
            new = FieldType.MAP if f.type is FieldType.LIST else r.choice((FieldType.DATETIME, FieldType.DATE))
            f = dataclasses.replace(f, type=new)
    if cfg.binary_probability > 0 and limits.max_binary_bytes > 0 and f.type is FieldType.STRING:
        r = _stream(seed, "binary", table, f.name)
        if r.random() < cfg.binary_probability:
            f = dataclasses.replace(f, type=FieldType.BINARY)
    if cfg.collate_probability > 0 and f.type is FieldType.STRING:
        r = _stream(seed, "collate", table, f.name)
        if r.random() < cfg.collate_probability:
            f = dataclasses.replace(f, case_insensitive=True)
    return f


def _tail_field_axes(seed: "int | None", cfg: GeneratorConfig, node_types: tuple, base: "NodeTypeSpec | None", limits: StorageLimits):
    if seed is None:
        return node_types, base
    out = []
    for nt in node_types:
        fields = tuple(_retype(seed, cfg, nt.name, f, limits) for f in nt.fields)
        out.append(dataclasses.replace(nt, fields=fields))
    if base is not None:
        base = dataclasses.replace(base, fields=tuple(_retype(seed, cfg, base.name, f, limits) for f in base.fields))
    return tuple(out), base


def _without_blob_indexes(indexes: list, node_types: tuple, base: "NodeTypeSpec | None") -> list:
    """No index covers a BINARY column: the indexes were drawn before the binary axis
    retyped the column (an index on a byte string is the engine's own affair, and how
    it keys one is not what the oracle models)."""
    types: dict = {}
    for nt in node_types + ((base,) if base is not None else ()):
        for f in nt.fields + (base.fields if base is not None and nt is not base else ()):
            types[(nt.name, f.name)] = f.type
    return [ix for ix in indexes if not any(types.get((ix.table, f)) in BLOB_TYPES for f in ix.all_fields)]


def _list_indexes(seed: "int | None", cfg: GeneratorConfig, node_types: tuple, base: "NodeTypeSpec | None", engine) -> list:
    if seed is None or cfg.list_index_probability <= 0:
        return []
    out = []
    for nt in list(node_types) + ([base] if base is not None else []):
        for f in nt.fields:
            if f.type not in (FieldType.LIST, FieldType.MAP):
                continue
            r = _stream(seed, "listindex", nt.name, f.name)
            if r.random() < cfg.list_index_probability:
                opts = engine.physical_design.draw_index_options(r, f.type)
                out.append(IndexSpec(table=nt.name, field=f.name, data_model=r.choice(engine.data_models + ("both",)), kind=r.choice(engine.physical_design.NON_UNIQUE_KINDS), ignore_nulls=r.random() < 0.5, options=opts))
    return out


def _dedupe_indexes(indexes: list, data_models: tuple) -> list:
    """Two specs rendering the same index NAME on some data model would fail that data model's
    setup alone (a fuzzer-made asymmetry); the later one is dropped."""
    seen: set = set()
    kept = []
    for ix in indexes:
        keys = {(s, ix.name()) for s in data_models if ix.applies_to(s)}
        if keys & seen:
            continue
        seen |= keys
        kept.append(ix)
    return kept


class _ValuePools:
    def __init__(self, boundaries: "dict | None" = None):
        self.pools: dict = {}
        self.boundaries = boundaries or BOUNDARY_VALUES

    def add(self, f: FieldSpec, v):
        self.pools.setdefault((f.name, f.type), []).append(v)

    def get(self, f: FieldSpec) -> list:
        return self.pools.get((f.name, f.type), [])


def _controlled_pool(seed: int, table: str, f: FieldSpec, boundaries: dict) -> list:
    rng = _stream(seed, "cardpool", table, f.name)
    out: list = []
    while len(out) < f.cardinality:
        v = boundary_value(rng, f.type, boundaries) if rng.random() < 0.3 else random_value(rng, f.type)
        if v not in out:
            out.append(v)
        if f.type is FieldType.BOOL and len(out) == 2:
            break
    return out


def _draw_value(rng: random.Random, f: FieldSpec, cfg: GeneratorConfig, pools: _ValuePools):
    roll = rng.random()
    if roll < cfg.boundary_value_probability:
        return boundary_value(rng, f.type, pools.boundaries)
    pool = pools.get(f)
    if pool and roll < cfg.boundary_value_probability + cfg.value_reuse_probability:
        return rng.choice(pool)
    return random_value(rng, f.type)


def _row_values(seed: int, table: str, fields: tuple, row: int, cfg: GeneratorConfig, pools: _ValuePools, controlled: dict, tag: str = "value", grow: "_LargeRows | None" = None) -> dict:
    values: dict = {}
    for f in fields:
        rng = _stream(seed, tag, table, row, f.name)
        r = rng.random()
        if r < cfg.null_probability:
            continue
        if r < cfg.null_probability + cfg.explicit_null_probability:
            values[f.name] = None
            continue
        if f.cardinality is not None:
            v = rng.choice(controlled[(table, f.name)])
        else:
            v = _draw_value(rng, f, cfg, pools)
        if f.case_insensitive and isinstance(v, str) and _stream(seed, "case", table, row, f.name).random() < 0.5:
            # a ci field holds case variants of its values on purpose
            v = v.upper() if any(c.islower() for c in v) else v.lower()
        values[f.name] = list(v) if isinstance(v, list) else (dict(v) if isinstance(v, dict) else v)
        pools.add(f, v)
    # the large-row axis grows AFTER pooling: a grown value is the row's alone, never
    # reused by another row (which would multiply the volume uncontrollably)
    return grow.grow(table, row, values, fields, tag) if grow is not None else values


# --- the large-row axis -----------------------------------------------------------------------

_GROWN_MIN = 256
# what a grown value costs the record, per unit of its size (a code point may be four
# UTF-8 bytes, base64 is four characters per three bytes, an integer element travels
# as up to 21 characters)
_UNIT_BYTES = {FieldType.STRING: 2, FieldType.BINARY: 2, FieldType.LIST: 8}


def _log_uniform(rng: random.Random, lo: int, hi: int) -> int:
    """A size between `lo` and `hi`, every order of magnitude equally likely: most
    grown values are kilobytes, a few are the limit itself."""
    if hi <= lo:
        return lo
    return min(hi, int(math.exp(rng.uniform(math.log(lo), math.log(hi)))))


def large_value(rng: random.Random, ftype: FieldType, size: int):
    """A value of `ftype` about `size` long (code points, bytes or elements), in a
    shape a storage engine treats differently from the others: one character repeated
    (compressible), random text or bytes (not), multi-byte code points (a string whose
    byte length, UTF-16 length and code-point length all differ), text full of the
    characters a serializer escapes, the same integer repeated, ascending integers."""
    if ftype is FieldType.STRING:
        kind = rng.choice(("run", "random", "unicode", "escaped", "mixed"))
        if kind == "run":
            return rng.choice("a Zz0") * size
        if kind == "random":
            return _b64(rng.randbytes(size * 3 // 4 + 3))[:size]
        if kind == "unicode":
            block = "".join(chr(c) for c in (0x4E2D, 0x6587, 0x1F600, 0xE9, 0x130, 0xDF, 0x200B, 0x4E00, 0xFFFF, 0x1F4A9))
            return (block * (size // len(block) + 1))[:size]
        if kind == "escaped":
            block = "\"'\\\n\t %_[]{}:$, `~!@&*()<>=|;?/"
            return (block * (size // len(block) + 1))[:size]
        head, tail = rng.choice(("", " ", "\n", "\"", "SELECT ")), rng.choice(("", " ", "\n", "\\", " FROM"))
        body = max(0, size - len(head) - len(tail))
        return head + _b64(rng.randbytes(body * 3 // 4 + 3))[:body] + tail
    if ftype is FieldType.BINARY:
        kind = rng.choice(("random", "zeros", "ones", "pattern"))
        if kind == "random":
            raw = rng.randbytes(size)
        elif kind == "zeros":
            raw = b"\x00" * size
        elif kind == "ones":
            raw = b"\xff" * size
        else:
            raw = (bytes(range(256)) * (size // 256 + 1))[:size]
        return _b64(raw)
    if ftype is FieldType.LIST:
        kind = rng.choice(("random", "same", "boundary", "ascending"))
        if kind == "random":
            return [rng.randint(-1000, 1000) for _ in range(size)]
        if kind == "same":
            return [rng.choice((0, 1, -1, I64_MAX, I64_MIN))] * size
        if kind == "boundary":
            pool = BOUNDARY_VALUES[FieldType.INT]
            return [rng.choice(pool) for _ in range(size)]
        start = rng.randint(-1000, 1000)
        return list(range(start, start + size))
    raise ValueError(ftype)


class _LargeRows:
    """The large-row axis (`large_row_probability`). A node row is large on its own
    tail stream (`large`); each present, uncontrolled STRING / BINARY / LIST value of a
    large row grows with probability 1/2 on the field's own stream (`largevalue`, per
    tag: a churned row's initial values grow independently) to a log-uniform size
    under the type's bound (`StorageLimits.value_cap`), the index key bound when an
    index of any data model covers the field, and what is left of the row's budget --
    a quarter of the record bound, so the grown values and the row's other fields
    never near it."""

    def __init__(self, seed: int, cfg: GeneratorConfig, schema: Schema, limits: StorageLimits):
        self.seed = seed
        self.probability = cfg.large_row_probability
        self.limits = limits
        self.schema = schema
        self._indexed: dict = {}

    def indexed(self, table: str, field_name: str) -> bool:
        """Whether an index of some data model keys the field (a composite's any
        component; the base class's indexes cover a subclass)."""
        key = (table, field_name)
        if key not in self._indexed:
            schema = self.schema
            tables = {table} | ({schema.base.name} if schema.base is not None else set())
            self._indexed[key] = any(ix.target == "node" and ix.table in tables and field_name in ix.all_fields for ix in schema.indexes)
        return self._indexed[key]

    def cap(self, table: str, f: FieldSpec, budget: int) -> int:
        cap = self.limits.value_cap(f.type)
        if self.indexed(table, f.name):
            # a key is the value's UTF-8 form plus the engine's overhead; a code point
            # may take four bytes
            key_cap = max(0, self.limits.max_index_key_bytes - 64)
            cap = min(cap, key_cap // 4 if f.type is FieldType.STRING else key_cap if f.type is FieldType.BINARY else cap)
        return min(cap, budget // _UNIT_BYTES[f.type])

    def grow(self, table: str, row: int, values: dict, fields: tuple, tag: str) -> dict:
        if self.probability <= 0 or _stream(self.seed, "large", table, row).random() >= self.probability:
            return values
        budget = self.limits.max_record_bytes // 4
        out = dict(values)
        for f in fields:
            if f.type not in GROWABLE_TYPES or f.cardinality is not None or out.get(f.name) is None:
                continue
            r = _stream(self.seed, "largevalue", tag, table, row, f.name)
            if r.random() >= 0.5:
                continue
            cap = self.cap(table, f, budget)
            if cap < _GROWN_MIN:
                continue
            size = _log_uniform(r, _GROWN_MIN, cap)
            out[f.name] = large_value(r, f.type, size)
            budget -= size * _UNIT_BYTES[f.type]
        return out


_KEY_STYLES = ("int", "punct", "word", "long", "unicode")
_KEY_PUNCT_BASES = ("k:1", "a.b", "x@y", "(p)", "c+d", "e,f", "g=h", "i;j", "$", "!", "*", "it's", "%", "dash-", "UPPER", "42", "a b", "q\"q")
_KEY_UNICODE_BASES = (chr(0x00E9), chr(0x1F600), chr(0x4E2D), chr(0x0130), "e" + chr(0x0301))


def _custom_key(seed: int, type_name: str, local_id: int, cfg: GeneratorConfig, is_valid_key) -> "str | None":
    if cfg.custom_key_probability <= 0:
        return None
    style_rng = _stream(seed, "keystyle", type_name)
    if style_rng.random() >= cfg.custom_key_probability:
        return None
    style = style_rng.choice(_KEY_STYLES)
    rng = _stream(seed, "key", type_name, local_id)
    if style == "int":
        key = str([local_id, 10**9 + local_id, 2**62 + local_id][local_id % 3])
    elif style == "punct":
        key = f"{rng.choice(_KEY_PUNCT_BASES)}{local_id}"
    elif style == "word":
        key = f"{rng.choice(('a', 'B', 'z', 'null', 'true', 'SELECT'))}{local_id}"
    elif style == "unicode":
        key = f"{rng.choice(_KEY_UNICODE_BASES)}{local_id}"
    else:
        key = "k" * rng.choice((100, 150, 190)) + str(local_id)
    assert is_valid_key(key), key
    return key


def _write_form(seed: int, data_model: str, cfg: GeneratorConfig, engine) -> WriteForm:
    """How one data model's setup writes its steps, on the data model's own stream;
    the forms' vocabulary is the engine's. The chunk size draws nothing."""
    rng = _stream(seed, "writeform", data_model)
    form = WriteForm() if rng.random() >= cfg.write_form_probability else engine.physical_design.draw_write_form(rng)
    return dataclasses.replace(form, chunk_size=cfg.statements_per_request) if cfg.statements_per_request else form


def generate_instance(seed: int, schema: Schema, cfg: GeneratorConfig) -> Instance:
    engine = schema.engine
    pd = engine.physical_design
    pools = _ValuePools(pd.BOUNDARY_VALUES)
    controlled = {
        (t.name, f.name): _controlled_pool(seed, t.name, f, pd.BOUNDARY_VALUES)
        for t in list(schema.node_types) + list(schema.edge_types)
        for f in t.fields
        if f.cardinality is not None
    }
    if schema.base is not None:
        # a base field's controlled pool is one pool shared by every subclass, so a
        # polymorphic GROUP BY / IN collides across classes on purpose
        for f in schema.base.fields:
            if f.cardinality is not None:
                pool = _controlled_pool(seed, schema.base.name, f, pd.BOUNDARY_VALUES)
                for nt in schema.node_types:
                    controlled[(nt.name, f.name)] = pool

    limits = limits_of(engine)
    grow = _LargeRows(seed, cfg, schema, limits)
    nodes: list = []
    churn: list = []
    for nt in schema.node_types:
        rng = _stream(seed, "rows", nt.name)
        lo = 0 if rng.random() < cfg.empty_table_probability else cfg.min_nodes_per_type
        count = rng.randint(lo, max(lo, cfg.max_nodes_per_type))
        fields = schema.query_fields(nt.name)
        for i in range(count):
            values = _row_values(seed, nt.name, fields, i, cfg, pools, controlled, grow=grow)
            node = NodeRow(type_name=nt.name, local_id=i, values=values, custom_key=_custom_key(seed, nt.name, i, cfg, pd.is_valid_key))
            nodes.append(node)
            crng = _stream(seed, "churn", nt.name, i)
            if crng.random() < cfg.churn_update_probability:
                initial = _row_values(seed, nt.name, fields, i, cfg, pools, controlled, tag="initvalue", grow=grow)
                churn.append(NodeUpdate(node=node, initial_values=initial))

    by_type = {nt.name: [n for n in nodes if n.type_name == nt.name] for nt in schema.node_types}
    edges: list = []
    # a source's fan-out stays under the engine's bound: a hub (or a pair's source)
    # that is full is passed over, which no default-size instance ever reaches
    fan_out_cap = limits.max_edges_per_node
    for et in schema.edge_types:
        rng = _stream(seed, "topology", et.name)
        sources, targets = by_type[et.from_type], by_type[et.to_type]
        if not sources or not targets:
            continue
        count = rng.randint(cfg.min_edges_per_type, cfg.max_edges_per_type)
        hub = rng.choice(sources)
        emitted: list = []
        fan_out: dict = {}
        for i in range(count):
            if emitted and rng.random() < cfg.parallel_edge_probability:
                src, dst = rng.choice(emitted)
            else:
                if rng.random() < cfg.hub_probability:
                    src = hub if fan_out.get(hub.record_id, 0) < fan_out_cap else rng.choice(sources)
                else:
                    src = rng.choice(sources)
                if et.from_type == et.to_type and rng.random() < cfg.self_loop_probability:
                    dst = src
                else:
                    dst = rng.choice(targets)
            if fan_out.get(src.record_id, 0) >= fan_out_cap:
                continue
            fan_out[src.record_id] = fan_out.get(src.record_id, 0) + 1
            emitted.append((src, dst))
            values = _row_values(seed, et.name, et.fields, i, cfg, pools, controlled)
            edge = EdgeRow(type_name=et.name, local_id=i, from_record=src.record_id, to_record=dst.record_id, values=values)
            edges.append(edge)
            if _stream(seed, "churn", et.name, i).random() < cfg.churn_delete_probability:
                churn.append(EdgeDelete(edge=edge))
            elif et.fields and cfg.churn_edge_update_probability > 0 and _stream(seed, "edgechurn", et.name, i).random() < cfg.churn_edge_update_probability:
                initial = _row_values(seed, et.name, et.fields, i, cfg, pools, controlled, tag="initvalue")
                churn.append(EdgeUpdate(edge=edge, initial_values=initial))

    deleted_edges = {op.edge.record_id for op in churn if isinstance(op, EdgeDelete)}
    edges = [e for e in edges if e.record_id not in deleted_edges]
    nodes, edges, churn = _apply_node_deletions(seed, nodes, edges, churn, cfg)

    instance = Instance(
        schema=schema,
        nodes=nodes,
        edges=edges,
        churn=churn,
        write_forms={data_model: _write_form(seed, data_model, cfg, engine) for data_model in engine.data_models},
    )
    if cfg.demote_colliding_indexes:
        instance.schema = _demote_colliding_indexes(instance)
    return instance


def _apply_node_deletions(seed: int, nodes: list, edges: list, churn: list, cfg: GeneratorConfig):
    if cfg.churn_node_delete_probability <= 0:
        return nodes, edges, churn
    doomed = [n for n in nodes if _stream(seed, "nodedelete", n.type_name, n.local_id).random() < cfg.churn_node_delete_probability]
    if not doomed:
        return nodes, edges, churn
    doomed_ids = {n.record_id for n in doomed}
    surviving_edges, cascaded = [], {n.record_id: [] for n in doomed}
    for e in edges:
        if e.from_record in doomed_ids:
            cascaded[e.from_record].append(e)
        elif e.to_record in doomed_ids:
            cascaded[e.to_record].append(e)
        else:
            surviving_edges.append(e)
    churn = list(churn) + [NodeDelete(node=n, incident_edges=tuple(cascaded[n.record_id])) for n in doomed]
    return [n for n in nodes if n.record_id not in doomed_ids], surviving_edges, churn


def initial_nodes(instance: Instance) -> list:
    """Every node that was ever written, including ones churn later deletes."""
    return list(instance.nodes) + [op.node for op in instance.churn if isinstance(op, NodeDelete)]


def initial_node_values(instance: Instance) -> dict:
    """record id -> the values the node was FIRST written with."""
    out = {n.record_id: n.values for n in initial_nodes(instance)}
    for op in instance.churn:
        if isinstance(op, NodeUpdate):
            out[op.node.record_id] = op.initial_values
    return out


def write_order_states(instance: Instance):
    """Every node-value state the setup passes through: all initial values, then one
    state per `NodeUpdate` in churn order, then the surviving rows."""
    state = dict(initial_node_values(instance))
    yield dict(state)
    finals = {n.record_id: n.values for n in initial_nodes(instance)}
    for op in instance.churn:
        if isinstance(op, NodeUpdate):
            state[op.node.record_id] = finals.get(op.node.record_id, op.node.values)
            yield dict(state)
    yield {n.record_id: n.values for n in instance.nodes}


def index_key(schema: Schema, ix: IndexSpec, values: dict, edge: bool = False) -> tuple:
    """The key an index holds for a record, as a hashable tuple (the engine says how a
    value is keyed: `physical_design.index_key_value`)."""
    _hashable = schema.engine.physical_design.index_key_value
    out = []
    for f in ix.all_fields:
        try:
            ci = schema.field(ix.table, f, edge=edge).case_insensitive
        except KeyError:
            ci = False
        out.append(_hashable(values.get(f), ci))
    return tuple(out)


def _collides(instance: Instance, ix: IndexSpec) -> bool:
    """Whether two rows of the class hold the same key in ANY write state (a unique
    index rejects the second write; one that replaces keeps only one of them): the
    states of `write_order_states`, walked incrementally -- the key multiset of the
    initial state, then each `NodeUpdate` moving one row's key -- so the check costs
    the rows plus the updates rather than their product. The final state (the
    surviving rows) is a subset of the last one and can add no collision."""
    fields = ix.all_fields
    schema = instance.schema
    base = schema.is_base(ix.table)
    prefix = ix.table + "/"

    def key_of(values: dict):
        if ix.ignore_nulls and any(values.get(f) is None for f in fields):
            return None
        return index_key(schema, ix, values)

    counts: dict = {}
    current: dict = {}
    for rid, values in initial_node_values(instance).items():
        if not base and not rid.startswith(prefix):
            continue
        key = key_of(values)
        current[rid] = key
        if key is not None:
            counts[key] = counts.get(key, 0) + 1
            if counts[key] > 1:
                return True
    finals = {n.record_id: n.values for n in initial_nodes(instance)}
    for op in instance.churn:
        if not isinstance(op, NodeUpdate):
            continue
        rid = op.node.record_id
        if rid not in current:
            continue
        old = current[rid]
        if old is not None:
            counts[old] -= 1
        key = key_of(finals.get(rid, op.node.values))
        current[rid] = key
        if key is not None:
            counts[key] = counts.get(key, 0) + 1
            if counts[key] > 1:
                return True
    return False


def edge_value_states(instance: Instance):
    """Every edge-attribute state the setup passes through: the initial values of
    every edge ever written, then the final ones."""

    edges = initial_edges(instance)
    yield initial_edge_values(instance)
    yield {e.record_id: e.values for e in edges}


def _edge_collides(instance: Instance, ix: IndexSpec) -> bool:

    ids = {e.record_id for e in initial_edges(instance) if e.type_name == ix.table}
    for state in edge_value_states(instance):
        seen = set()
        for eid, values in state.items():
            if eid not in ids:
                continue
            if ix.ignore_nulls and values.get(ix.field) is None:
                continue
            key = index_key(instance.schema, ix, values, edge=True)
            if key in seen:
                return True
            seen.add(key)
    return False


def _demote_colliding_indexes(instance: Instance) -> Schema:
    """An index that holds one record per key, over values that collide, becomes the
    kind its provider demotes it to. Judged over EVERY write state, in write order --
    the mixed states between two updates are where a collision can hide."""
    schema = instance.schema
    pd = schema.engine.physical_design
    fixed = []
    for ix in schema.indexes:
        if pd.holds_one_record_per_key(ix):
            collides = _edge_collides(instance, ix) if ix.target == "edge" else _collides(instance, ix)
            if collides:
                ix = dataclasses.replace(ix, kind=pd.demoted_kind(ix.kind))
        fixed.append(ix)
    return dataclasses.replace(schema, indexes=tuple(fixed))


def build(seed: int, cfg: GeneratorConfig, engine) -> "tuple[Schema, Instance, QueryRng]":
    cfg = _scale.resolve(cfg, limits_of(engine))
    schema = generate_schema(_stream(seed, "schema"), cfg, seed=seed, engine=engine)
    instance = generate_instance(seed, schema, cfg)
    return instance.schema, instance, QueryRng(seed)
