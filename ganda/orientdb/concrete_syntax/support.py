"""The building blocks of the SELECT and PATH IRs: a predicate's literal concrete syntax,
the AND over a composite index's fields, the `index:` door, the method catalog with
its Python mirrors, the per-key sort key, the transaction door's rows and the BFS
distances."""
from __future__ import annotations

import dataclasses

from ganda.core.query.ir.select import TxWrite
from ganda.core.data.model import FieldType, Instance, NodeRow, Schema
from ganda.orientdb.storage.dialect import HASH_KINDS, sql_op
from ganda.orientdb.facade import predicates
from ganda.orientdb.facade.predicates import Compound, Predicate
from ganda.core.query.generators.support import bfs_distances, composite_and as _composite_and, draw_tx, tx_binds, tx_rows  # noqa: F401
from ganda.orientdb.rules import assumes, works_around
from ganda.orientdb.rules.catalog import REFUSED, SQL, INDEX_DOOR



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
        return f"{p.field} {sql_op(p.op)}"
    pivots = p.pivot if isinstance(p.pivot, list) else [p.pivot]
    if p.op == "LIKE" or any(isinstance(v, str) or isinstance(v, list) or v is None for v in pivots):
        return None
    if p.op == "BETWEEN":
        return f"{p.field} BETWEEN {_literal(p.pivot[0])} AND {_literal(p.pivot[1])}"
    return f"{p.field} {sql_op(p.op)} {_literal(p.pivot)}"




# The findings a class SCAN shares with every other path of the same question --
# the ones a reversed (`:p > f`) or arithmetic (`f + :c > :p`) concrete syntax inherits
# from the plain predicate (the index-served ones it does not: it is never served).
SCAN_SIDE_FINDINGS = ("O7", "O17", "O27", "O32")


# the `index:` door answers as an index-served lookup, takes no record filter, and is
# asked `=`, IN and BETWEEN only (a range through it is O3)
assumes("equivalent.index_door", INDEX_DOOR, True)
assumes("accepts.record_filter", INDEX_DOOR, REFUSED)
works_around("O3")


def index_door(p, table: str, schema: Schema, data_model: str) -> "tuple[str, dict, object] | None":
    """`SELECT rid.id AS id FROM index:<name> WHERE key ...` for a leaf `=` / `IN` /
    `BETWEEN` on a field the table itself indexes on `data_model` (a base-class index
    covers other classes and the door cannot filter by class, probed), or `key =
    [a, b]` for an AND of equalities over a composite index's fields (a prefix of
    them too). A range through the door is O3's ClassCastException, a BETWEEN through
    a hash index an O5 501: neither is emitted."""
    own = [ix for ix in schema.indexes if ix.target == "node" and ix.table == table and ix.applies_to(data_model) and ix.option("by") is None]
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
            return f"SELECT rid.id AS id FROM index:{ix.name()} WHERE key {sql_op(p.op)} :{p.bind}", {}, ix
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


# Methods, each with a guard on the PRESENT values it is asked over (the query filters
# `IS NOT NULL`). Casing is asked over ASCII only (Java's default-locale toUpperCase is
# locale-dependent beyond it).
METHODS = {
    FieldType.STRING: [
        ("{f}.toUpperCase()", lambda s: s.isascii()),
        ("{f}.toLowerCase()", lambda s: s.isascii()),
        ("{f}.length()", lambda s: True),
        ("{f}.trim()", lambda s: s.isascii()),
        ("{f}.append('x')", lambda s: True),
        ("{f}.prefix('x')", lambda s: True),
        ("{f}.left(1)", lambda s: all(ord(c) < 0x10000 for c in s)),
        ("{f}.indexOf('a')", lambda s: True),
        ("{f}.replace('a', 'b')", lambda s: True),
    ],
    FieldType.INT: [
        ("{f}.asString()", lambda v: True),
        ("{f} + 1", lambda v: v < 2**63 - 1),
        ("{f} - 1", lambda v: v > -(2**63)),
        ("{f} * 2", lambda v: abs(v) < 2**62),
        ("{f} / 2", lambda v: abs(v) < 2**53),
        ("{f} % 3", lambda v: True),
        ("math_abs({f})", lambda v: v > -(2**63)),
        ("{f}.asLong()", lambda v: True),
    ],
    FieldType.FLOAT: [
        ("{f} * 2", lambda v: abs(v) < 8e307),
        ("{f} + 0", lambda v: True),
        ("math_abs({f})", lambda v: True),
        ("{f}.asLong()", lambda v: abs(v) < 2**62),
    ],
    FieldType.BOOL: [
        ("{f}.asString()", lambda v: True),
    ],
    FieldType.LIST: [
        ("{f}.size()", lambda v: True),
        ("{f}.asSet().size()", lambda v: True),
    ],
    # INTEGER arithmetic (probed): `+` / `-` promote to a long on overflow, `*` WRAPS
    # in 32 bits and `math_abs(-2^31)` is -2^31 (Java), so those are mirrored only
    # where no wrap can happen
    FieldType.INT32: [
        ("{f}.asString()", lambda v: True),
        ("{f} + 1", lambda v: True),
        ("{f} - 1", lambda v: True),
        ("{f} * 2", lambda v: abs(v) < 2**30),
        ("{f} / 2", lambda v: True),
        ("{f} % 3", lambda v: True),
        ("math_abs({f})", lambda v: v > -(2**31)),
        ("{f}.asLong()", lambda v: True),
    ],
    # FLOAT: exact for the dyadic values the generator stores; `Float.toString`
    # matches Python's repr only without an exponent (1e-3 <= |v| < 1e7)
    FieldType.FLOAT32: [
        ("{f} * 2", lambda v: abs(v) < 2**20),
        ("{f} + 0", lambda v: True),
        ("math_abs({f})", lambda v: True),
        ("{f}.asLong()", lambda v: abs(v) < 2**62),
        ("{f}.asString()", lambda v: v == 0 or 1e-3 <= abs(v) < 1e7),
    ],
    # a DATETIME / DATE: its epoch milliseconds, its formatted parts (Java's
    # SimpleDateFormat patterns on the UTC value), a day of arithmetic
    FieldType.DATETIME: [
        ("{f}.asLong()", lambda v: True),
        ("{f}.format('yyyy-MM-dd')", lambda v: True),
        ("{f}.format('yyyy')", lambda v: True),
        ("{f}.format('HH:mm:ss')", lambda v: True),
        ("{f}.format('yyyy-MM-dd HH:mm:ss')", lambda v: True),
        ("{f} + 86400000", lambda v: v < "9999-12-31"),
        ("{f}.asDate().format('yyyy-MM-dd HH:mm:ss')", lambda v: True),
    ],
    FieldType.DATE: [
        ("{f}.asLong()", lambda v: True),
        ("{f}.format('yyyy-MM-dd')", lambda v: True),
        ("{f}.format('yyyy')", lambda v: True),
        ("{f} + 86400000", lambda v: v < "9999-12-31"),
    ],
    FieldType.MAP: [
        ("{f}.size()", lambda v: True),
        ("{f}.keys().size()", lambda v: True),
        ("{f}.values().size()", lambda v: True),
    ],
    # DECIMAL: BigDecimal arithmetic, read back as the double of its decimal string;
    # `math_abs` has no BigDecimal overload (rejected, probed) so it is not mirrored
    FieldType.DECIMAL: [
        ("{f} * 2", lambda v: abs(v) < 1e15),
        ("{f} + 0", lambda v: True),
        ("{f} - 1", lambda v: abs(v) < 1e15),
        ("{f}.asLong()", lambda v: abs(v) < 2**62),
        ("{f}.asString()", lambda v: v != 0 and 1e-3 <= abs(v) < 1e7),
    ],
}


# --- the transaction door ---------------------------------------------------------------------

assumes("accepts.delete_from.vertex_or_edge_class", SQL, REFUSED)


def tx_write(tx: TxWrite, data_model: str) -> str:
    """The write's text on `data_model`: a vertex is created and deleted through `CREATE
    VERTEX` / `DELETE VERTEX` (a `DELETE FROM` a vertex class is refused)."""
    t, f = tx.table, tx.field
    if tx.write == "delete":
        return f"DELETE VERTEX {t} WHERE id = :tid" if data_model == "graph" else f"DELETE FROM {t} WHERE id = :tid"
    if tx.write == "insert":
        return ("CREATE VERTEX" if data_model == "graph" else "INSERT INTO") + f" {t} SET id = :tid, {f} = :tv"
    if tx.remove:
        return f"UPDATE {t} REMOVE {f} WHERE id = :tid"
    return f"UPDATE {t} SET {f} = :tv WHERE id = :tid"


def composite_and(rng, fields, values_of):
    from ganda.orientdb.provider import PROVIDER

    return _composite_and(rng, fields, values_of, PROVIDER)
