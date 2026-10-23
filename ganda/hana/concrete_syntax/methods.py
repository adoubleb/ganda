"""The method catalog of the select IR's `method` kind: an expression over one present
field, as SQL (column tables and the doc views) and as SPARQL, with the guard on the
values it is asked over.

A template is the catalog's neutral name with `{f}` for the field (`upper({f})`); `sql`
and `sparql` give each data model's text. Strings are guarded to the Basic Multilingual
Plane and the case functions to ASCII (Unicode case mapping is the engine's own).
"""
from __future__ import annotations

from ganda.core.constants import I64_MAX
from ganda.core.data.model import FieldType


def _bmp(s: str) -> bool:
    return all(ord(c) < 0x10000 for c in s)


# template -> (sql, sparql, guard) per type
CATALOG = {
    FieldType.STRING: {
        "upper({f})": ("UPPER({f})", "UCASE({f})", lambda s: s.isascii()),
        "lower({f})": ("LOWER({f})", "LCASE({f})", lambda s: s.isascii()),
        "length({f})": ("LENGTH({f})", "STRLEN({f})", _bmp),
        "concat({f})": ("{f} || 'x'", 'CONCAT({f}, "x")', _bmp),
        "first({f})": ("SUBSTRING({f}, 1, 1)", "SUBSTR({f}, 1, 1)", _bmp),
    },
    FieldType.INT: {
        "plus1({f})": ("({f} + 1)", "({f} + 1)", lambda v: v < I64_MAX),
        "times2({f})": ("({f} * 2)", "({f} * 2)", lambda v: abs(v) < 2**62),
        "abs({f})": ("ABS({f})", "ABS({f})", lambda v: v > -I64_MAX),
        "neg({f})": ("(-{f})", "(-{f})", lambda v: v > -I64_MAX),
    },
    FieldType.FLOAT: {
        # the constant a double literal: a double times an integer is a double in SPARQL,
        # an engine that promotes to decimal instead would round
        "times2({f})": ("({f} * 2)", "({f} * 2.0e0)", lambda v: abs(v) < 8e307 and (v == 0 or abs(v) > 1e-300)),
        "abs({f})": ("ABS({f})", "ABS({f})", lambda v: True),
        "neg({f})": ("(-{f})", "(-{f})", lambda v: v != 0),
    },
}

# the core's view: type -> [(template, guard)]
METHODS = {ftype: [(t, guard) for t, (_s, _q, guard) in entries.items()] for ftype, entries in CATALOG.items()}


def result_type(template: str, ftype: FieldType) -> FieldType:
    """The type of a method's answer: the field's own, but a string's length is an int."""
    return FieldType.INT if template == "length({f})" else ftype


def sql_text(template: str, ftype: FieldType, expr: str) -> str:
    return CATALOG[ftype][template][0].format(f=expr)


def sparql_text(template: str, ftype: FieldType, expr: str) -> str:
    return CATALOG[ftype][template][1].format(f=expr)
