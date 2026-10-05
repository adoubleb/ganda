"""Random, seeded generation of schemas and data instances.

Identifiers are synthetic (`n1`, `e1`, `f1`, `r0`) and every generation axis (schema
shape, topology, values, churn, indexes, keys, write forms, the id index) draws from
its own named RNG sub-stream (`_stream`) derived from the instance seed -- never one
shared `random.Random(seed)` consumed in call order. Two consequences the rest of the
pipeline relies on:

- adding a draw to one axis cannot reshuffle another axis's output, so a before/after
  mutation-benchmark comparison A/Bs exactly the axis that changed;
- row `i`'s value for field `f` depends only on `(seed, type, i, f)`, so shrinking a
  bound in the reducer removes rows instead of regenerating a different instance.

Axes that can be switched off (custom keys, write forms, node deletion, the id index
axis, inheritance, the narrow numeric types, `ci` collation, list indexes, clusters,
edge-attribute churn) draw from *tail* streams keyed by their own name, so at
probability 0 they reproduce exactly what the generator produced without them.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import random
from dataclasses import dataclass

from ganda.orientdb.model import (
    MAP_KEYS,
    NUMERIC_TYPES,
    DATA_MODELS,
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
from ganda.orientdb.dialect import (
    EDGE_FROM_FORMS,
    EDGE_UPDATE_FORMS,
    ID_INDEX_KINDS,
    INSERT_FORMS,
    TRANSACTION_FORMS,
    UPDATE_FORMS,
    is_valid_key,
)


def _stream(seed: int, *parts) -> random.Random:
    """One named, independently-seeded RNG sub-stream. `random.Random` hashes a str
    seed with a fixed, un-salted algorithm, so this is deterministic across processes
    (unlike the builtin `hash()`, which must never be used here)."""
    return random.Random(f"{seed}:{':'.join(str(p) for p in parts)}")


class QueryRng(random.Random):
    """The rng handed to `all_cases()`; `for_generator(name)` gives each query generator its own
    independent sub-stream, so a query generator can be sampled, skipped or added without
    perturbing any other query generator's draws, and the reducer can reconstruct the exact
    case that failed."""

    def __init__(self, seed: int):
        super().__init__(f"{seed}:queries")
        self._template_seed = seed

    def for_generator(self, name: str) -> random.Random:
        return _stream(self._template_seed, "queries", name)


_FIELD_TYPE_WEIGHTS = {
    FieldType.INT: 3,
    FieldType.FLOAT: 2,
    FieldType.STRING: 3,
    FieldType.BOOL: 1,
    FieldType.LIST: 1,
}
_CARDINALITY_CLASSES = (2, 3, 5)

I64_MAX = 2**63 - 1
I64_MIN = -(2**63)
F64_INT_EXACT = 2**53
DBL_MAX = 1.7976931348623157e308

# Adversarial per-type value pools (all live-probed to store and read back unchanged on
# 3.2.56 through a declared property; NaN/Infinity are excluded because JSON cannot
# carry them). Strings are built with `chr()` where an editor could normalize them.
_BOUNDARY_VALUES = {
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
    # int32 values: the box boundaries (an index converts a Long key by wrapping, so
    # 2^31 - 1 next to -2^31 is where `f > 2^31` answers wrong, O16)
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
# A value shaped like a RID (`#1:0`) is deliberately NOT in the string pool: the SQL
# layer converts it to a LINK inside an embedded document (probed), so it would make
# the doc data model's link entries type-divergent by construction. It belongs to a
# schemaless-class axis, not to this pool.

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
    if ftype is FieldType.BOOL:
        return rng.random() < 0.5
    if ftype is FieldType.LIST:
        return [rng.randint(-100, 100) for _ in range(rng.randint(0, _ARRAY_MAX_LEN))]
    raise ValueError(ftype)


def boundary_value(rng: random.Random, ftype: FieldType):
    v = rng.choice(_BOUNDARY_VALUES[ftype])
    if isinstance(v, dict):
        return dict(v)
    return list(v) if isinstance(v, list) else v


@dataclass
class GeneratorConfig:
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
    # A field is omitted from a row (absent; `IS NULL` true, `IS DEFINED` false).
    null_probability: float = 0.1
    # An explicit stored `null` (`IS NULL` true, `IS DEFINED` true).
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
    # from `_INDEX_KIND_WEIGHTS`.
    index_probability: float = 0.35
    composite_index_probability: float = 0.15
    # Per edge type: an index on one edge attribute (graph E class / junction class).
    edge_index_probability: float = 0.2
    # Clears `unique` on an index whose generated values collide and turns a
    # DICTIONARY index on a column with duplicate values into NOTUNIQUE (O4). Draws
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
    id_index_probability: float = 0.5
    # An abstract base class every node class extends, with 1-3 fields of its own
    # (`b1`, ...) that every node carries, and indexes on those fields placed on the
    # base or on one subclass (`Schema.base`). At 0 no base class exists.
    inheritance_probability: float = 0.0
    # Per node field: an INT field becomes an INTEGER, a FLOAT field a FLOAT or a
    # DECIMAL column (`FieldType.INT32` / `FLOAT32` / `DECIMAL`). The row streams are
    # unchanged, so only the retyped column's values move.
    narrow_type_probability: float = 0.0
    # Per node field: an INT field becomes a DATETIME or a DATE column, a LIST field
    # an EMBEDDEDMAP LONG (`FieldType.DATETIME` / `DATE` / `MAP`); tail streams too.
    date_map_probability: float = 0.0
    # Per STRING node field: `COLLATE ci`, and every stored value of the field is
    # case-flipped with probability 0.5 so case variants collide.
    collate_probability: float = 0.0
    # Per node class: 2 or 3 clusters (`CREATE CLASS ... CLUSTERS n`).
    clusters_probability: float = 0.0
    # Per LIST node field: a NOTUNIQUE / NOTUNIQUE_HASH_INDEX index on the list (an
    # index over the elements: `CONTAINSANY` and a list `=` / `IN` are served by it,
    # `CONTAINS` is not, probed). Per MAP node field: the same, `BY KEY` (serves
    # CONTAINSKEY) or `BY VALUE` (serves CONTAINSVALUE).
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
    # the write phase (`ganda.orientdb.write_phase`, `ir_write`): this many predicate-targeted
    # writes after every read case, each checked (answers, state, read-back) and
    # applied to the Instance; its own sub-stream, 0 reproduces a run from before it
    write_cases: int = 8
    # LLM-written template cards (`fuzzer/cards`, query generator `llm_card`): this many card
    # instantiations per instance from the JSONL corpus at `card_corpus`, its own
    # sub-stream after every other read supply; 0 reproduces a run from before it
    card_cases: int = 0
    card_corpus: str = ""

    def with_(self, **kw) -> "GeneratorConfig":
        return dataclasses.replace(self, **kw)


_INDEX_KIND_WEIGHTS = {"NOTUNIQUE": 40, "UNIQUE": 10, "NOTUNIQUE_HASH_INDEX": 20, "UNIQUE_HASH_INDEX": 5, "DICTIONARY": 15}


def _random_field_specs(rng: random.Random, count: int, cfg: GeneratorConfig, prefix: str = "f") -> tuple:
    specs = []
    for i in range(count):
        (ftype,) = rng.choices(list(_FIELD_TYPE_WEIGHTS), weights=list(_FIELD_TYPE_WEIGHTS.values()))
        cardinality = rng.choice(_CARDINALITY_CLASSES) if rng.random() < cfg.cardinality_control_probability else None
        specs.append(FieldSpec(name=f"{prefix}{i + 1}", type=ftype, cardinality=cardinality))
    return tuple(specs)


BASE_CLASS = "N0"


def _base_class(seed: "int | None", cfg: GeneratorConfig, node_names: list) -> "tuple[NodeTypeSpec | None, list]":
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
        indexes.append(IndexSpec(table=table, field=f.name, data_model=r.choice(("doc", "graph", "rel", "both")), kind=_draw_kind(r), ignore_nulls=r.random() < 0.5))
    return base, indexes


def _draw_kind(rng: random.Random) -> str:
    (kind,) = rng.choices(list(_INDEX_KIND_WEIGHTS), weights=list(_INDEX_KIND_WEIGHTS.values()))
    return kind


def generate_schema(rng: random.Random, cfg: GeneratorConfig, seed: "int | None" = None) -> Schema:
    n_node_types = rng.randint(cfg.min_node_types, cfg.max_node_types)
    node_types = tuple(
        NodeTypeSpec(name=f"n{i + 1}", fields=_random_field_specs(rng, rng.randint(cfg.min_fields_per_type, cfg.max_fields_per_type), cfg))
        for i in range(n_node_types)
    )
    names = [nt.name for nt in node_types]
    n_edge_types = rng.randint(cfg.min_edge_types, cfg.max_edge_types)
    edge_types = tuple(
        EdgeTypeSpec(
            name=f"e{i + 1}",
            from_type=rng.choice(names),
            to_type=rng.choice(names),
            fields=_random_field_specs(rng, rng.randint(0, cfg.max_fields_per_type), cfg),
        )
        for i in range(n_edge_types)
    )

    data_models = ("doc", "graph", "rel", "both")
    indexes = []
    for nt in node_types:
        scalar = [f for f in nt.fields if f.type is not FieldType.LIST]
        for f in scalar:
            if rng.random() < cfg.index_probability:
                indexes.append(IndexSpec(table=nt.name, field=f.name, data_model=rng.choice(data_models), kind=_draw_kind(rng), ignore_nulls=rng.random() < 0.5))
        if len(scalar) >= 2 and rng.random() < cfg.composite_index_probability:
            a, b = rng.sample(scalar, 2)
            indexes.append(IndexSpec(table=nt.name, field=a.name, extra_fields=(b.name,), data_model=rng.choice(data_models), kind=rng.choice(("NOTUNIQUE", "NOTUNIQUE_HASH_INDEX"))))
    for et in edge_types:
        scalar = [f for f in et.fields if f.type is not FieldType.LIST]
        if scalar and rng.random() < cfg.edge_index_probability:
            indexes.append(IndexSpec(table=et.name, field=rng.choice(scalar).name, data_model=rng.choice(("graph", "rel", "both")), kind=rng.choice(("NOTUNIQUE", "NOTUNIQUE_HASH_INDEX")), target="edge"))

    id_index: dict = {}
    if seed is not None and cfg.id_index_probability > 0:
        for data_model in DATA_MODELS:
            r = _stream(seed, "idindex", data_model)
            if r.random() < cfg.id_index_probability:
                id_index[data_model] = r.choice(ID_INDEX_KINDS)
    base, base_indexes = _base_class(seed, cfg, names)
    node_types, base = _tail_field_axes(seed, cfg, node_types, base)
    list_indexes = _list_indexes(seed, cfg, node_types, base)
    return Schema(node_types=node_types, edge_types=tuple(edge_types), indexes=tuple(_dedupe_indexes(indexes + base_indexes + list_indexes)), id_index=id_index, base=base)


def _retype(seed: "int | None", cfg: GeneratorConfig, table: str, f: FieldSpec) -> FieldSpec:
    """The narrow-type and collation tail axes for one node field."""
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
    if cfg.collate_probability > 0 and f.type is FieldType.STRING:
        r = _stream(seed, "collate", table, f.name)
        if r.random() < cfg.collate_probability:
            f = dataclasses.replace(f, collate="ci")
    return f


def _tail_field_axes(seed: "int | None", cfg: GeneratorConfig, node_types: tuple, base: "NodeTypeSpec | None"):
    if seed is None:
        return node_types, base
    out = []
    for nt in node_types:
        fields = tuple(_retype(seed, cfg, nt.name, f) for f in nt.fields)
        clusters = 1
        if cfg.clusters_probability > 0:
            r = _stream(seed, "clusters", nt.name)
            if r.random() < cfg.clusters_probability:
                clusters = r.choice((2, 3))
        out.append(dataclasses.replace(nt, fields=fields, clusters=clusters))
    if base is not None:
        base = dataclasses.replace(base, fields=tuple(_retype(seed, cfg, base.name, f) for f in base.fields))
    return tuple(out), base


def _list_indexes(seed: "int | None", cfg: GeneratorConfig, node_types: tuple, base: "NodeTypeSpec | None") -> list:
    if seed is None or cfg.list_index_probability <= 0:
        return []
    out = []
    for nt in list(node_types) + ([base] if base is not None else []):
        for f in nt.fields:
            if f.type not in (FieldType.LIST, FieldType.MAP):
                continue
            r = _stream(seed, "listindex", nt.name, f.name)
            if r.random() < cfg.list_index_probability:
                by = r.choice(("KEY", "VALUE")) if f.type is FieldType.MAP else None
                out.append(IndexSpec(table=nt.name, field=f.name, data_model=r.choice(("doc", "graph", "rel", "both")), kind=r.choice(("NOTUNIQUE", "NOTUNIQUE_HASH_INDEX")), ignore_nulls=r.random() < 0.5, by=by))
    return out


def _dedupe_indexes(indexes: list) -> list:
    """Two specs rendering the same index NAME on some data model would fail that data model's
    setup alone (a fuzzer-made asymmetry); the later one is dropped."""
    seen: set = set()
    kept = []
    for ix in indexes:
        keys = {(s, ix.name()) for s in DATA_MODELS if ix.applies_to(s)}
        if keys & seen:
            continue
        seen |= keys
        kept.append(ix)
    return kept


class _ValuePools:
    def __init__(self):
        self.pools: dict = {}

    def add(self, f: FieldSpec, v):
        self.pools.setdefault((f.name, f.type), []).append(v)

    def get(self, f: FieldSpec) -> list:
        return self.pools.get((f.name, f.type), [])


def _controlled_pool(seed: int, table: str, f: FieldSpec) -> list:
    rng = _stream(seed, "cardpool", table, f.name)
    out: list = []
    while len(out) < f.cardinality:
        v = boundary_value(rng, f.type) if rng.random() < 0.3 else random_value(rng, f.type)
        if v not in out:
            out.append(v)
        if f.type is FieldType.BOOL and len(out) == 2:
            break
    return out


def _draw_value(rng: random.Random, f: FieldSpec, cfg: GeneratorConfig, pools: _ValuePools):
    roll = rng.random()
    if roll < cfg.boundary_value_probability:
        return boundary_value(rng, f.type)
    pool = pools.get(f)
    if pool and roll < cfg.boundary_value_probability + cfg.value_reuse_probability:
        return rng.choice(pool)
    return random_value(rng, f.type)


def _row_values(seed: int, table: str, fields: tuple, row: int, cfg: GeneratorConfig, pools: _ValuePools, controlled: dict, tag: str = "value") -> dict:
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
        if f.ci and isinstance(v, str) and _stream(seed, "case", table, row, f.name).random() < 0.5:
            # a ci field holds case variants of its values on purpose
            v = v.upper() if any(c.islower() for c in v) else v.lower()
        values[f.name] = list(v) if isinstance(v, list) else (dict(v) if isinstance(v, dict) else v)
        pools.add(f, v)
    return values


_KEY_STYLES = ("int", "punct", "word", "long", "unicode")
_KEY_PUNCT_BASES = ("k:1", "a.b", "x@y", "(p)", "c+d", "e,f", "g=h", "i;j", "$", "!", "*", "it's", "%", "dash-", "UPPER", "42", "a b", "q\"q")
_KEY_UNICODE_BASES = (chr(0x00E9), chr(0x1F600), chr(0x4E2D), chr(0x0130), "e" + chr(0x0301))


def _custom_key(seed: int, type_name: str, local_id: int, cfg: GeneratorConfig) -> "str | None":
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


def _write_form(seed: int, data_model: str, cfg: GeneratorConfig) -> WriteForm:
    rng = _stream(seed, "writeform", data_model)
    if rng.random() >= cfg.write_form_probability:
        return WriteForm()
    form = WriteForm(
        insert_via=rng.choice(INSERT_FORMS),
        update_form=rng.choice(UPDATE_FORMS[:4]),
        edge_from=rng.choice(EDGE_FROM_FORMS),
        transaction=rng.choice(TRANSACTION_FORMS),
        cancelled_junk=rng.random() < 0.5,
    )
    # the later doors draw AFTER the original ones, so an older seed keeps its forms
    if rng.random() < 0.25:
        form = dataclasses.replace(form, update_form="arith")
    return dataclasses.replace(form, edge_update_form=rng.choice(EDGE_UPDATE_FORMS), rebuild_indexes=rng.random() < 0.3)


def generate_instance(seed: int, schema: Schema, cfg: GeneratorConfig) -> Instance:
    pools = _ValuePools()
    controlled = {
        (t.name, f.name): _controlled_pool(seed, t.name, f)
        for t in list(schema.node_types) + list(schema.edge_types)
        for f in t.fields
        if f.cardinality is not None
    }
    if schema.base is not None:
        # a base field's controlled pool is one pool shared by every subclass, so a
        # polymorphic GROUP BY / IN collides across classes on purpose
        for f in schema.base.fields:
            if f.cardinality is not None:
                pool = _controlled_pool(seed, schema.base.name, f)
                for nt in schema.node_types:
                    controlled[(nt.name, f.name)] = pool

    nodes: list = []
    churn: list = []
    for nt in schema.node_types:
        rng = _stream(seed, "rows", nt.name)
        lo = 0 if rng.random() < cfg.empty_table_probability else cfg.min_nodes_per_type
        count = rng.randint(lo, max(lo, cfg.max_nodes_per_type))
        fields = schema.query_fields(nt.name)
        for i in range(count):
            values = _row_values(seed, nt.name, fields, i, cfg, pools, controlled)
            node = NodeRow(type_name=nt.name, local_id=i, values=values, custom_key=_custom_key(seed, nt.name, i, cfg))
            nodes.append(node)
            crng = _stream(seed, "churn", nt.name, i)
            if crng.random() < cfg.churn_update_probability:
                initial = _row_values(seed, nt.name, fields, i, cfg, pools, controlled, tag="initvalue")
                churn.append(NodeUpdate(node=node, initial_values=initial))

    by_type = {nt.name: [n for n in nodes if n.type_name == nt.name] for nt in schema.node_types}
    edges: list = []
    for et in schema.edge_types:
        rng = _stream(seed, "topology", et.name)
        sources, targets = by_type[et.from_type], by_type[et.to_type]
        if not sources or not targets:
            continue
        count = rng.randint(cfg.min_edges_per_type, cfg.max_edges_per_type)
        hub = rng.choice(sources)
        emitted: list = []
        for i in range(count):
            if emitted and rng.random() < cfg.parallel_edge_probability:
                src, dst = rng.choice(emitted)
            else:
                src = hub if rng.random() < cfg.hub_probability else rng.choice(sources)
                if et.from_type == et.to_type and rng.random() < cfg.self_loop_probability:
                    dst = src
                else:
                    dst = rng.choice(targets)
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
        doc_write_form=_write_form(seed, "doc", cfg),
        graph_write_form=_write_form(seed, "graph", cfg),
        rel_write_form=_write_form(seed, "rel", cfg),
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


def _hashable(v, ci: bool = False):
    if isinstance(v, bool):
        return ("b", v)
    if isinstance(v, float):
        return ("f", v, str(v)[0] == "-")
    if isinstance(v, list):
        return tuple(_hashable(x) for x in v)
    if isinstance(v, dict):
        return tuple(sorted((k, _hashable(x)) for k, x in v.items()))
    if ci and isinstance(v, str):
        # a ci index stores the lower-cased key (probed: a UNIQUE index rejects `A`
        # next to `a`)
        return v.lower()
    return v


def index_key(schema: Schema, ix: IndexSpec, values: dict, edge: bool = False) -> tuple:
    """The key an index holds for a record, as a hashable tuple (ci fields lowered)."""
    out = []
    for f in ix.all_fields:
        try:
            ci = schema.field(ix.table, f, edge=edge).ci
        except KeyError:
            ci = False
        out.append(_hashable(values.get(f), ci))
    return tuple(out)


def _collides(instance: Instance, ix: IndexSpec) -> bool:
    """Whether two rows of the class hold the same key in ANY write state (a unique
    index rejects the second write; a DICTIONARY index keeps one of them, O4)."""
    fields = ix.all_fields
    base = instance.schema.is_base(ix.table)
    for state in write_order_states(instance):
        seen = set()
        for rid, values in state.items():
            if not base and not rid.startswith(ix.table + "/"):
                continue
            if ix.ignore_nulls and any(values.get(f) is None for f in fields):
                continue
            key = index_key(instance.schema, ix, values)
            if key in seen:
                return True
            seen.add(key)
    return False


def edge_value_states(instance: Instance):
    """Every edge-attribute state the setup passes through: the initial values of
    every edge ever written, then the final ones."""
    from ganda.orientdb.mapping.common import initial_edge_values, initial_edges

    edges = initial_edges(instance)
    yield initial_edge_values(instance)
    yield {e.record_id: e.values for e in edges}


def _edge_collides(instance: Instance, ix: IndexSpec) -> bool:
    from ganda.orientdb.mapping.common import initial_edges

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
    """Unique kinds whose values collide become their NOTUNIQUE sibling; a DICTIONARY
    index over duplicate values becomes NOTUNIQUE (the planner would answer an
    equality predicate through it and drop rows, issue-drafts/O4). Judged over EVERY
    write state, in write order -- the mixed states between two updates are where a
    collision can hide (the ArangoDB port learned that on run6 seed 998235)."""
    schema = instance.schema
    fixed = []
    for ix in schema.indexes:
        if ix.kind in ("UNIQUE", "UNIQUE_HASH_INDEX", "DICTIONARY"):
            collides = _edge_collides(instance, ix) if ix.target == "edge" else _collides(instance, ix)
            if collides:
                ix = dataclasses.replace(ix, kind={"UNIQUE": "NOTUNIQUE", "UNIQUE_HASH_INDEX": "NOTUNIQUE_HASH_INDEX", "DICTIONARY": "NOTUNIQUE"}[ix.kind])
        fixed.append(ix)
    return dataclasses.replace(schema, indexes=tuple(fixed))


def build(seed: int, cfg: GeneratorConfig) -> "tuple[Schema, Instance, TemplateRng]":
    """The single entry point: (schema, instance, query rng) for a (seed, config)."""
    schema = generate_schema(_stream(seed, "schema"), cfg, seed=seed)
    instance = generate_instance(seed, schema, cfg)
    return instance.schema, instance, QueryRng(seed)
