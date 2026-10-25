"""The building blocks of the SELECT and PATH IRs: the AND over a composite index's
fields, the method catalog with its Python mirrors, the per-key sort key, the transaction door's rows and the BFS
distances."""
from __future__ import annotations


from ganda.core.query.ir.select import TxWrite
from ganda.core.data.model import FieldType
from ganda.core.query.generators.support import bfs_distances, composite_and as _composite_and, draw_tx, tx_binds, tx_rows  # noqa: F401


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
