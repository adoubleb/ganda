"""The building blocks of the SELECT and PATH IRs: the method catalog with its guards,
the transaction door's write, the AND over a composite index's fields, the BFS
distances."""
from __future__ import annotations

from ganda.core.data.model import FieldType, Schema
from ganda.core.query.generators.support import bfs_distances, composite_and as _composite_and, draw_tx, tx_binds, tx_rows  # noqa: F401
from ganda.core.query.ir.select import TxWrite
from ganda.surrealdb.engine.dialect import engine_id, surql_literal

# Methods, each with a guard on the PRESENT values it is asked over (the query filters
# the field's presence: a function over a missing value is refused). Casing is asked
# over ASCII only (Unicode case mappings differ by library in the special cases).
METHODS = {
    FieldType.STRING: [
        ("string::uppercase({f})", lambda s: s.isascii()),
        ("string::lowercase({f})", lambda s: s.isascii()),
        ("{f}.uppercase()", lambda s: s.isascii()),
        ("string::len({f})", lambda s: True),
        ("string::trim({f})", lambda s: s.isascii()),
        ("string::concat({f}, 'x')", lambda s: True),
        ("string::reverse({f})", lambda s: True),
        ("string::contains({f}, 'a')", lambda s: True),
        ("string::replace({f}, 'a', 'b')", lambda s: True),
        ("string::slice({f}, 0, 1)", lambda s: True),
    ],
    FieldType.INT: [
        ("<string>{f}", lambda v: True),
        ("{f} + 1", lambda v: v < 2**63 - 1),
        ("{f} - 1", lambda v: v > -(2**63)),
        ("{f} * 2", lambda v: abs(v) < 2**62),
        ("{f} % 3", lambda v: True),
        ("math::abs({f})", lambda v: v > -(2**63)),
        ("<float>{f}", lambda v: abs(v) < 2**53),
    ],
    FieldType.FLOAT: [
        ("{f} * 2", lambda v: abs(v) < 8e307),
        ("{f} + 0", lambda v: True),
        ("math::abs({f})", lambda v: True),
        ("math::floor({f})", lambda v: abs(v) < 2**62),
    ],
    FieldType.BOOL: [
        ("<string>{f}", lambda v: True),
        ("!{f}", lambda v: True),
    ],
    FieldType.LIST: [
        ("array::len({f})", lambda v: True),
        ("array::len(array::distinct({f}))", lambda v: True),
        ("array::sort({f})", lambda v: True),
        ("array::first({f})", lambda v: True),
    ],
    FieldType.INT32: [
        ("<string>{f}", lambda v: True),
        ("{f} + 1", lambda v: True),
        ("{f} - 1", lambda v: True),
        ("{f} * 2", lambda v: True),
        ("{f} % 3", lambda v: True),
        ("math::abs({f})", lambda v: True),
    ],
    FieldType.FLOAT32: [
        ("{f} * 2", lambda v: True),
        ("{f} + 0", lambda v: True),
        ("math::abs({f})", lambda v: True),
    ],
    FieldType.DATETIME: [
        ("time::year({f})", lambda v: True),
        ("time::format({f}, '%Y-%m-%d')", lambda v: True),
        ("time::unix({f})", lambda v: True),
        ("{f} + 1d", lambda v: v < "9999-12-30"),
        ("time::hour({f})", lambda v: True),
    ],
    FieldType.DATE: [
        ("time::year({f})", lambda v: True),
        ("time::format({f}, '%Y-%m-%d')", lambda v: True),
        ("{f} + 1d", lambda v: v < "9999-12-30"),
    ],
    FieldType.MAP: [
        ("object::len({f})", lambda v: True),
        ("array::len(object::keys({f}))", lambda v: True),
        ("array::sort(object::values({f}))", lambda v: True),
    ],
    FieldType.DECIMAL: [
        ("{f} * 2", lambda v: abs(v) < 1e15),
        ("{f} + 0", lambda v: True),
        ("<string>{f}", lambda v: True),
        ("math::abs({f})", lambda v: True),
    ],
}


def tx_write(tx: TxWrite, data_model: str, schema: Schema) -> str:
    """The transaction door's write, the same text on every data model (a record is
    addressed by its id; the graph data model's delete cascades its edges, the
    others keep their junction rows and link entries -- the read inside the
    transaction asks the class alone)."""
    t, f = tx.table, tx.field
    try:
        ftype = schema.field(t, f).type
    except KeyError:
        ftype = None
    if tx.write == "delete":
        return f"DELETE {engine_id(tx.tid)}"
    if tx.write == "insert":
        return f"CREATE {engine_id(tx.tid)} SET {f} = {surql_literal(tx.tv, ftype)}"
    if tx.remove:
        return f"UPDATE {engine_id(tx.tid)} UNSET {f}"
    return f"UPDATE {engine_id(tx.tid)} SET {f} = {surql_literal(tx.tv, ftype)}"


def composite_and(rng, fields, values_of):
    from ganda.surrealdb.provider import PROVIDER

    return _composite_and(rng, fields, values_of, PROVIDER)
