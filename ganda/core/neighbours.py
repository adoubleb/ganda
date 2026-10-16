"""The neighbours of a stored value: the value one step above or below it, within the
type's range, and the bounds of the integer types.

A pivot "ADJACENT" to a stored value (bugs live at boundaries) is built here, as are the
providers' own pivot and semantics code, which share the 32/64-bit bounds, the float32
narrowing and the date shift. Every function is pure and engine-free.
"""
from __future__ import annotations

import math
import struct

from ganda.core.constants import I32_MAX, I32_MIN, I64_MAX, I64_MIN
from ganda.core.model import FieldType



def _f32(v: float) -> float:
    try:
        return struct.unpack("f", struct.pack("f", v))[0]
    except OverflowError:
        return v


def shift_date(v: str, seconds: int) -> str:
    import datetime as _dt

    try:
        t = _dt.datetime.strptime(v, "%Y-%m-%d %H:%M:%S") + _dt.timedelta(seconds=seconds)
    except (ValueError, OverflowError):
        return v
    if t.year < 1 or t.year > 9999:
        return v
    return t.strftime("%Y-%m-%d %H:%M:%S")


def offset(ftype: FieldType, v, direction: int):
    if ftype is FieldType.INT:
        nxt = v + direction
        return nxt if I64_MIN <= nxt <= I64_MAX else v
    if ftype is FieldType.INT32:
        nxt = v + direction
        return nxt if I32_MIN <= nxt <= I32_MAX else v
    if ftype is FieldType.FLOAT:
        nxt = math.nextafter(v, math.inf if direction > 0 else -math.inf)
        return nxt if math.isfinite(nxt) else v
    if ftype is FieldType.FLOAT32:
        # the neighbouring float32, widened (sent as an exact double)
        bits = struct.unpack("i", struct.pack("f", v))[0]
        if v == 0:
            nxt = _f32(math.nextafter(0.0, math.inf if direction > 0 else -math.inf))
            return nxt
        step = direction if v > 0 else -direction
        try:
            nxt = struct.unpack("f", struct.pack("i", bits + step))[0]
        except (struct.error, OverflowError):
            return v
        return nxt if math.isfinite(nxt) else v
    if ftype is FieldType.DECIMAL:
        return round(v + 0.001 * direction, 3)
    if ftype is FieldType.DATETIME:
        return shift_date(v, direction)
    if ftype is FieldType.DATE:
        return shift_date(v, 86400 * direction)
    if ftype is FieldType.STRING:
        # the code-unit successor; the predecessor of a non-empty string is its
        # longest proper prefix (a strictly smaller string in every order)
        return v + chr(0) if direction > 0 else (v[:-1] if v else v)
    return v
