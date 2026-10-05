"""The JDK hash codes of the engine's result rows, for the DISTINCT guard (O41).

`SELECT DISTINCT` dedups through a `HashSet<OResult>`; a projected row is an
`OResultInternal` whose `hashCode` is its content map's (`sum(key.hashCode() ^
value.hashCode())`) and whose `compareTo` answers 1 for any two non-record rows.
Once a bin of the set's table holds 9 rows (a table of at least 64) the JDK
treeifies it and searches the tree by `compareTo` among rows of EQUAL hash, so a
row equal to one already seen is missed -- and emitted again -- when the bin holds
two distinct rows of the same full hash (`issue-drafts/upstream/O41-...`).

`java_hash(value, ftype)` is the hash of the Java box the engine holds for a
declared column (LONG -> Long, INTEGER -> Integer, DOUBLE -> Double, FLOAT -> Float,
DECIMAL -> BigDecimal, DATETIME / DATE -> java.util.Date, lists and maps of Longs),
or None where the model does not know it (a DECIMAL outside plain notation, a date
before the Gregorian cutover): `distinct_collides` then counts it as colliding.
"""

from __future__ import annotations

import calendar
import struct
from datetime import datetime

from ganda.orientdb.model import FieldType

_M32 = 0xFFFFFFFF
_M64 = 0xFFFFFFFFFFFFFFFF


def i32(x: int) -> int:
    x &= _M32
    return x - (1 << 32) if x & 0x80000000 else x


def long_hash(v: int) -> int:
    u = v & _M64
    return i32(u ^ (u >> 32))


def string_hash(s: str) -> int:
    h = 0
    b = s.encode("utf-16-be", "surrogatepass")
    for i in range(0, len(b), 2):
        h = (31 * h + ((b[i] << 8) | b[i + 1])) & _M32
    return i32(h)


def _double_hash(v: float) -> int:
    if v != v:
        return long_hash(0x7FF8000000000000)
    return long_hash(struct.unpack(">q", struct.pack(">d", v))[0])


def _float_hash(v: float) -> int:
    if v != v:
        return 0x7FC00000
    return struct.unpack(">i", struct.pack(">f", v))[0]


def _decimal_hash(v) -> "int | None":
    """`BigDecimal.valueOf(double)` -- `new BigDecimal(Double.toString(d))` -- hashed:
    `31 * unscaled.hashCode() + scale` in its compact form. Only plain notation
    (1e-3 <= |d| < 1e7, or zero) is modelled: Python's repr agrees with Java there."""
    d = float(v)
    if d != 0 and not (1e-3 <= abs(d) < 1e7):
        return None
    text = repr(d)
    if "e" in text or "E" in text or "n" in text:
        return None
    neg = text.startswith("-")
    digits = text.lstrip("-")
    whole, frac = digits.split(".")
    unscaled = int(whole + frac)
    scale = len(frac)
    val2 = unscaled
    temp = i32(i32((val2 >> 32) & _M32) * 31 + (val2 & _M32))
    return i32(31 * (-temp if neg else temp) + scale)


def _date_hash(s: str) -> "int | None":
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    if dt.year < 1583:
        return None  # java.util.Date is Julian before the cutover
    ms = calendar.timegm(dt.timetuple()) * 1000
    return i32((ms & _M32) ^ ((ms >> 32) & _M32))


def java_hash(v, ftype: "FieldType | None") -> "int | None":
    if v is None:
        return 0
    if isinstance(v, bool):
        return 1231 if v else 1237
    if ftype is FieldType.INT32 and isinstance(v, int):
        return i32(v)
    if ftype is FieldType.FLOAT32:
        return _float_hash(float(v))
    if ftype is FieldType.FLOAT:
        return _double_hash(float(v))
    if ftype is FieldType.DECIMAL:
        return _decimal_hash(v)
    if ftype in (FieldType.DATETIME, FieldType.DATE):
        return _date_hash(v)
    if ftype is FieldType.LIST and isinstance(v, list):
        h = 1
        for e in v:
            eh = java_hash(e, FieldType.INT)
            if eh is None:
                return None
            h = (31 * h + eh) & _M32
        return i32(h)
    if ftype is FieldType.MAP and isinstance(v, dict):
        h = 0
        for k, e in v.items():
            eh = java_hash(e, FieldType.INT)
            if eh is None:
                return None
            h = (h + (string_hash(str(k)) ^ eh)) & _M32
        return i32(h)
    if isinstance(v, str):
        return string_hash(v)
    if isinstance(v, int):
        return long_hash(v)
    if isinstance(v, float):
        return _double_hash(v)
    return None


def row_hash(row: dict, types: dict) -> "int | None":
    """`OResultInternal.hashCode()`: its LinkedHashMap's -- the sum of `key ^ value`."""
    h = 0
    for k, v in row.items():
        vh = java_hash(v, types.get(k))
        if vh is None:
            return None
        h = (h + (string_hash(k) ^ vh)) & _M32
    return i32(h)


def _spread(h: int) -> int:
    u = h & _M32
    return u ^ (u >> 16)


TREEIFY = 9  # the ninth row appended to a bin treeifies it (TREEIFY_THRESHOLD 8)


def distinct_collides(rows: list, types: dict) -> bool:
    """Whether a DISTINCT over `rows` (the distinct rows, as dicts keyed by the
    projected aliases) may emit a duplicate: two distinct rows of equal hash share
    a bin that holds at least nine rows at some table size of at least 64. Which
    rows the table holds at each size depends on the engine's row order, so every
    row is counted at every size (conservative). A row whose hash the model does not
    know counts as colliding once there are nine rows."""
    if len(rows) < TREEIFY:
        return False
    hashes = [row_hash(r, types) for r in rows]
    if any(h is None for h in hashes):
        return True
    counts: dict = {}
    for h in hashes:
        counts[h] = counts.get(h, 0) + 1
    shared = [h for h, c in counts.items() if c >= 2]
    if not shared:
        return False
    # the sizes the table passes through once it holds at least half the rows (it
    # resizes past three quarters full; the duplicates the bug inserts grow it up to
    # twice): a smaller table holds a small order-dependent subset, and a bin that is
    # full there only when every row is counted is a random pile-up the order rarely
    # builds. The collisions the engine meets are STRUCTURED -- hashes sharing their
    # low bits: null, 0 and -1 all hash 0, 2^31, 2^63-1 and Long.MIN hash MIN_VALUE,
    # dyadic floats end in zero bits (campaign cov400 seed 5025 duplicated 0.0 at
    # size 128 with 162 distinct rows)
    size = 64
    while size * 3 // 4 < (len(rows) + 1) // 2:
        size *= 2
    while True:
        bins: dict = {}
        for h in hashes:
            b = _spread(h) & (size - 1)
            bins[b] = bins.get(b, 0) + 1
        if any(bins[_spread(h) & (size - 1)] >= TREEIFY for h in shared):
            return True
        if size * 3 // 4 >= 2 * len(rows):
            return False
        size *= 2
