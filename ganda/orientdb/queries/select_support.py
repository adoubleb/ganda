"""The building blocks of the SELECT and PATH IRs: a predicate's literal spelling,
the AND over a composite index's fields, the `index:` door, the method catalog with
its Python mirrors, the per-key sort key, the transaction door's rows and the BFS
distances."""
from __future__ import annotations

import dataclasses

from ganda.orientdb.model import FieldType, Instance, NodeRow, Schema
from ganda.orientdb.orient import HASH_KINDS, UNIQUE_KINDS
from ganda.orientdb.queries import predicates
from ganda.orientdb.queries.predicates import Compound, Predicate
from ganda.orientdb.reference import decimal_of, orient_key


def _hits(instance: Instance, table: str, p) -> list:
    return [n for n in instance.nodes_of(table) if predicates.evaluate(n.values, p)]


def _literal(v) -> str:
    """A NUMERIC pivot as SQL text -- the one kind of value that is safe to interpolate
    (a string never is). A float keeps its full repr."""
    if isinstance(v, list):
        return "[" + ", ".join(_literal(x) for x in v) + "]"
    if isinstance(v, bool):
        return "true" if v else "false"
    return repr(v)


def literal_pred(p) -> "str | None":
    if isinstance(p, Compound):
        parts = [literal_pred(c) for c in p.children]
        if any(x is None for x in parts):
            return None
        return f"NOT ({parts[0]})" if p.op == "NOT" else "(" + f" {p.op} ".join(parts) + ")"
    if p.key is not None or p.op in predicates.MAP_OPS or p.sub is not None:
        return None
    if p.op in predicates.NO_BIND_OPS:
        return f"{p.field} {p.op}"
    pivots = p.pivot if isinstance(p.pivot, list) else [p.pivot]
    if p.op == "LIKE" or any(isinstance(v, str) or isinstance(v, list) or v is None for v in pivots):
        return None
    if p.op == "BETWEEN":
        return f"{p.field} BETWEEN {_literal(p.pivot[0])} AND {_literal(p.pivot[1])}"
    return f"{p.field} {p.op} {_literal(p.pivot)}"


def composite_and(rng, fields, values_of) -> "Compound | None":
    children = []
    for k, f in enumerate(fields):
        leaf = predicates.draw(rng, "source", f, values_of(f), f"p_{k}")
        if leaf is None:
            return None
        children.append(leaf)
    return Compound("source", "AND", tuple(children)) if len(children) >= 2 else None


# The findings a class SCAN shares with every other path of the same question --
# the ones a reversed (`:p > f`) or arithmetic (`f + :c > :p`) spelling inherits
# from the plain predicate (the index-served ones it does not: it is never served).
SCAN_SIDE_FINDINGS = ("O7", "O17", "O27", "O32")


def index_door(p, table: str, schema: Schema, side: str) -> "tuple[str, dict, object] | None":
    """`SELECT rid.id AS id FROM index:<name> WHERE key ...` for a leaf `=` / `IN` /
    `BETWEEN` on a field the table itself indexes on `side` (a base-class index
    covers other classes and the door cannot filter by class, probed), or `key =
    [a, b]` for an AND of equalities over a composite index's fields (a prefix of
    them too). A range through the door is O3's ClassCastException, a BETWEEN through
    a hash index an O5 501: neither is emitted."""
    own = [ix for ix in schema.indexes if ix.target == "node" and ix.table == table and ix.applies_to(side) and ix.by is None]
    if isinstance(p, Predicate):
        if p.key is not None or p.op not in ("=", "IN", "BETWEEN") or p.ftype in ("list", "map"):
            return None
        for ix in own:
            if ix.field != p.field:
                continue
            if ix.extra_fields:
                # a prefix key: an SB-tree composite answers it, a hash composite
                # answers nothing (c100c seed 865)
                if p.op != "=" or ix.kind in HASH_KINDS:
                    continue
                return f"SELECT rid.id AS id FROM index:{ix.name()} WHERE key = :{p.bind}k", {f"{p.bind}k": [p.pivot]}, ix
            if p.op == "BETWEEN":
                if ix.kind in HASH_KINDS:
                    continue
                return f"SELECT rid.id AS id FROM index:{ix.name()} WHERE key BETWEEN :{p.bind}a AND :{p.bind}b", {}, ix
            return f"SELECT rid.id AS id FROM index:{ix.name()} WHERE key {p.op} :{p.bind}", {}, ix
        return None
    if isinstance(p, Compound) and p.op == "AND" and all(isinstance(c, Predicate) and c.op == "=" and c.key is None for c in p.children):
        by_field = {c.field: c for c in p.children}
        for ix in own:
            if not ix.extra_fields or set(by_field) != set(ix.all_fields):
                continue
            bind = p.children[0].bind.rsplit("_", 1)[0] + "k"
            return f"SELECT rid.id AS id FROM index:{ix.name()} WHERE key = :{bind}", {bind: [by_field[f].pivot for f in ix.all_fields]}, ix
    return None


def door_complete(ix, table: str, p, instance: Instance) -> bool:
    """Whether the `index:` door answers every record the predicate matches: an index
    with `ignoreNullValues: true` holds no entry for a record with a null in ANY of
    its fields, so a prefix key `[a]` of a composite misses the matching records
    whose second field is null or absent (probed: the planner scans such a prefix
    instead of using the index, smoke sel-smoke3 seed 1066)."""
    if not ix.ignore_nulls or not ix.extra_fields:
        return True
    return not any(any(n.values.get(g) is None for g in ix.all_fields) for n in _hits(instance, table, p))


# Methods with an exact Python mirror over PRESENT values (the query filters
# `IS NOT NULL`). Casing mirrors are only trusted over ASCII (Java's default-locale
# toUpperCase and Python's upper agree there; `reference_ok` withholds the rest).
def _epoch_ms(v: str) -> int:
    import datetime as _dt

    return int((_dt.datetime.strptime(v, "%Y-%m-%d %H:%M:%S") - _dt.datetime(1970, 1, 1)).total_seconds()) * 1000


def _shift(v: str, seconds: int) -> str:
    import datetime as _dt

    return (_dt.datetime.strptime(v, "%Y-%m-%d %H:%M:%S") + _dt.timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")


def _java_div(a: int, b: int) -> int:
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


def _java_mod(a: int, b: int) -> int:
    return a - _java_div(a, b) * b


METHODS = {
    FieldType.STRING: [
        ("{f}.toUpperCase()", lambda s: s.upper(), lambda s: s.isascii()),
        ("{f}.toLowerCase()", lambda s: s.lower(), lambda s: s.isascii()),
        ("{f}.length()", lambda s: len(s.encode("utf-16-be")) // 2, lambda s: True),
        ("{f}.trim()", lambda s: s.strip(" \t\n\r\x0b\x0c"), lambda s: s.isascii()),
        ("{f}.append('x')", lambda s: s + "x", lambda s: True),
        ("{f}.prefix('x')", lambda s: "x" + s, lambda s: True),
        ("{f}.left(1)", lambda s: s[:1], lambda s: all(ord(c) < 0x10000 for c in s)),
        ("{f}.indexOf('a')", lambda s: s.encode("utf-16-be").find("a".encode("utf-16-be")) // 2 if "a" in s else -1, lambda s: True),
        ("{f}.replace('a', 'b')", lambda s: s.replace("a", "b"), lambda s: True),
    ],
    FieldType.INT: [
        ("{f}.asString()", lambda v: str(v), lambda v: True),
        ("{f} + 1", lambda v: v + 1, lambda v: v < 2**63 - 1),
        ("{f} - 1", lambda v: v - 1, lambda v: v > -(2**63)),
        ("{f} * 2", lambda v: v * 2, lambda v: abs(v) < 2**62),
        ("{f} / 2", lambda v: v // 2 if v % 2 == 0 else v / 2, lambda v: abs(v) < 2**53),
        ("{f} % 3", lambda v: _java_mod(v, 3), lambda v: True),
        ("math_abs({f})", lambda v: abs(v), lambda v: v > -(2**63)),
        ("{f}.asLong()", lambda v: v, lambda v: True),
    ],
    FieldType.FLOAT: [
        ("{f} * 2", lambda v: v * 2, lambda v: abs(v) < 8e307),
        ("{f} + 0", lambda v: v + 0, lambda v: True),
        ("math_abs({f})", lambda v: abs(v), lambda v: True),
        ("{f}.asLong()", lambda v: int(v), lambda v: abs(v) < 2**62),
    ],
    FieldType.BOOL: [
        ("{f}.asString()", lambda v: "true" if v else "false", lambda v: True),
    ],
    FieldType.LIST: [
        ("{f}.size()", lambda v: len(v), lambda v: True),
        ("{f}.asSet().size()", lambda v: len(set(v)), lambda v: True),
    ],
    # INTEGER arithmetic (probed): `+` / `-` promote to a long on overflow, `*` WRAPS
    # in 32 bits and `math_abs(-2^31)` is -2^31 (Java), so those are mirrored only
    # where no wrap can happen
    FieldType.INT32: [
        ("{f}.asString()", lambda v: str(v), lambda v: True),
        ("{f} + 1", lambda v: v + 1, lambda v: True),
        ("{f} - 1", lambda v: v - 1, lambda v: True),
        ("{f} * 2", lambda v: v * 2, lambda v: abs(v) < 2**30),
        ("{f} / 2", lambda v: v // 2 if v % 2 == 0 else v / 2, lambda v: True),
        ("{f} % 3", lambda v: _java_mod(v, 3), lambda v: True),
        ("math_abs({f})", lambda v: abs(v), lambda v: v > -(2**31)),
        ("{f}.asLong()", lambda v: v, lambda v: True),
    ],
    # FLOAT: exact for the dyadic values the generator stores; `Float.toString`
    # matches Python's repr only without an exponent (1e-3 <= |v| < 1e7)
    FieldType.FLOAT32: [
        ("{f} * 2", lambda v: v * 2, lambda v: abs(v) < 2**20),
        ("{f} + 0", lambda v: v + 0, lambda v: True),
        ("math_abs({f})", lambda v: abs(v), lambda v: True),
        ("{f}.asLong()", lambda v: int(v), lambda v: abs(v) < 2**62),
        ("{f}.asString()", lambda v: repr(v), lambda v: v == 0 or 1e-3 <= abs(v) < 1e7),
    ],
    # a DATETIME / DATE: its epoch milliseconds, its formatted parts (Java's
    # SimpleDateFormat patterns on the UTC value), a day of arithmetic
    FieldType.DATETIME: [
        ("{f}.asLong()", lambda v: _epoch_ms(v), lambda v: True),
        ("{f}.format('yyyy-MM-dd')", lambda v: v[:10], lambda v: True),
        ("{f}.format('yyyy')", lambda v: v[:4], lambda v: True),
        ("{f}.format('HH:mm:ss')", lambda v: v[11:], lambda v: True),
        ("{f}.format('yyyy-MM-dd HH:mm:ss')", lambda v: v, lambda v: True),
        ("{f} + 86400000", lambda v: _shift(v, 86400), lambda v: v < "9999-12-31"),
        ("{f}.asDate().format('yyyy-MM-dd HH:mm:ss')", lambda v: v[:10] + " 00:00:00", lambda v: True),
    ],
    FieldType.DATE: [
        ("{f}.asLong()", lambda v: _epoch_ms(v), lambda v: True),
        ("{f}.format('yyyy-MM-dd')", lambda v: v[:10], lambda v: True),
        ("{f}.format('yyyy')", lambda v: v[:4], lambda v: True),
        ("{f} + 86400000", lambda v: _shift(v, 86400), lambda v: v < "9999-12-31"),
    ],
    FieldType.MAP: [
        ("{f}.size()", lambda v: len(v), lambda v: True),
        ("{f}.keys().size()", lambda v: len(v), lambda v: True),
        ("{f}.values().size()", lambda v: len(v), lambda v: True),
    ],
    # DECIMAL: BigDecimal arithmetic, read back as the double of its decimal string;
    # `math_abs` has no BigDecimal overload (rejected, probed) so it is not mirrored
    FieldType.DECIMAL: [
        ("{f} * 2", lambda v: float(decimal_of(v) * 2), lambda v: abs(v) < 1e15),
        ("{f} + 0", lambda v: v, lambda v: True),
        ("{f} - 1", lambda v: float(decimal_of(v) - 1), lambda v: abs(v) < 1e15),
        ("{f}.asLong()", lambda v: int(v), lambda v: abs(v) < 2**62),
        ("{f}.asString()", lambda v: str(decimal_of(v)), lambda v: v != 0 and 1e-3 <= abs(v) < 1e7),
    ],
}


def tx_rows(instance: Instance, p: dict) -> list:
    """The class's rows with the transaction's write applied."""
    rows = list(instance.nodes_of(p["table"]))
    write, tid = p["write"], p["tid"]
    if write == "delete":
        return [n for n in rows if n.record_id != tid]
    if write == "insert":
        return rows + [NodeRow(p["table"], -1, {p["field"]: p["tv"]}, custom_key="tx0")]
    out = []
    for n in rows:
        if n.record_id != tid:
            out.append(n)
            continue
        values = dict(n.values)
        if p["remove"]:
            values.pop(p["field"], None)
        else:
            values[p["field"]] = p["tv"]
        out.append(dataclasses.replace(n, values=values))
    return out


def bfs_distances(instance: Instance, start: str, direction: str, edge_type: "str | None") -> dict:
    """Record id -> hops of a shortest path from `start` over the edges of
    `edge_type` (every type when None) in `direction`; the start itself at 0."""
    adj: dict = {}
    for e in instance.edges:
        if edge_type is not None and e.type_name != edge_type:
            continue
        if direction in ("OUT", "BOTH"):
            adj.setdefault(e.from_record, []).append(e.to_record)
        if direction in ("IN", "BOTH"):
            adj.setdefault(e.to_record, []).append(e.from_record)
    dist = {start: 0}
    frontier = [start]
    while frontier:
        nxt = []
        for u in frontier:
            for w in adj.get(u, []):
                if w not in dist:
                    dist[w] = dist[u] + 1
                    nxt.append(w)
        frontier = nxt
    return dist


def order_key(v, desc: bool):
    """A per-key sort key with OrientDB's null placement (last ascending, first
    descending) and the key order reversed for DESC."""
    if v is None:
        return (0 if desc else 1, ())
    k = orient_key(v)
    if desc:
        return (1, negate_key(k))
    return (0, k)


def negate_key(k):
    """The reverse of `orient_key`'s order for a numeric or boolean value."""
    if k[0] == 1:
        return (-k[1],)
    if k[0] == 2:
        return (-k[1], -k[2])
    raise ValueError(k)


def method_fn(template: str, ftype: FieldType):
    for t, fn, _ in METHODS[ftype]:
        if t == template:
            return fn
    raise KeyError(template)


# --- the transaction door ---------------------------------------------------------------------

TX_WRITES = ("update", "update", "insert", "delete")


def _unique_on_any_side(schema: Schema, table: str, field_name: str) -> bool:
    return any(ix.kind in UNIQUE_KINDS for side in ("doc", "graph", "rel") for ix in schema.node_indexes(table, field_name, side))


def draw_tx(rng, schema: Schema, instance: Instance, table: str, where) -> "dict | None":
    """One write for the transaction door (`SelectQuery.tx`): a record of `table` updated
    (a field set -- most often one the filter reads -- set to null, or removed),
    inserted with one field, or deleted. A value reused from another record is never
    written to a column a UNIQUE index of any side covers (a duplicate key inside
    the transaction raises nothing before COMMIT, and the unique lookup answers one
    of the two)."""
    from ganda.orientdb.generator import random_value

    fields = schema.query_fields(table)
    rows = instance.nodes_of(table)
    if schema.is_base(table) or not fields or not rows:
        return None
    write = rng.choice(TX_WRITES)
    target = rng.choice(rows)
    mentioned = [f for f in fields if f.name in predicates.fields_of(where)]
    f = rng.choice(mentioned) if mentioned and rng.random() < 0.75 else rng.choice(fields)
    tx = {"table": table, "write": write, "field": f.name, "tid": target.record_id, "tv": None, "remove": False}
    if write == "delete":
        return tx
    roll = rng.random()
    present = [n.values.get(f.name) for n in rows if n.values.get(f.name) is not None]
    unique = _unique_on_any_side(schema, table, f.name)
    if write == "update" and roll < 0.15:
        tx["remove"] = True
    elif write == "update" and roll < 0.25:
        pass
    elif present and roll < 0.55 and not unique:
        tx["tv"] = rng.choice(present)
    else:
        v = random_value(rng, f.type)
        if unique:
            for _ in range(10):
                if all(orient_key(v) != orient_key(x) for x in present):
                    break
                v = random_value(rng, f.type)
            else:
                v, tx["remove"] = None, True
        tx["tv"] = v
    if write == "insert":
        tx["tid"] = f"{table}/tx0"
        tx["remove"] = False
    return tx


def tx_binds(tx: dict) -> dict:
    return {"tid": tx["tid"]} if tx["write"] == "delete" else {"tid": tx["tid"], "tv": tx["tv"]}


def tx_write(tx: dict, side: str) -> str:
    """The write's text on `side`: a vertex is created and deleted through `CREATE
    VERTEX` / `DELETE VERTEX` (a `DELETE FROM` a vertex class is refused)."""
    t, f = tx["table"], tx["field"]
    if tx["write"] == "delete":
        return f"DELETE VERTEX {t} WHERE id = :tid" if side == "graph" else f"DELETE FROM {t} WHERE id = :tid"
    if tx["write"] == "insert":
        return ("CREATE VERTEX" if side == "graph" else "INSERT INTO") + f" {t} SET id = :tid, {f} = :tv"
    if tx["remove"]:
        return f"UPDATE {t} REMOVE {f} WHERE id = :tid"
    return f"UPDATE {t} SET {f} = :tv WHERE id = :tid"
