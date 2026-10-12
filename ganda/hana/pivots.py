"""SAP HANA's side of predicate sampling: the operators a leaf may draw and the pivots it
asks with.

The null tests are `IS NULL` / `IS NOT NULL` only: an absent field and a stored null
are one null on every data model (`dialect`). A CROSS-TYPE pivot (the core's
`cross_type_probability`) is a double against an INT column (`v ± 0.5`) or an integer
against a FLOAT column, kept below 10^12 in magnitude: every data model then compares it
as Python compares an int with a float -- SQL with the parameter typed by the pivot
(`CAST(? AS DOUBLE)`: a bare `?` takes the column's type and truncates), the collection
filter with the pivot's cast, SPARQL by numeric promotion, openCypher with both sides
`TOFLOAT(...)` (probe R21 / D24 / K24 / G36) -- and the bound keeps the engines' BIGINT
to DOUBLE conversions harmless (2^63 - 1 against a double pivot of 2^63 parts them, G36's
`999999999999.5` against 2^63 - 1 does not) and a cypher constant under A8's fifteen
digits.
"""
from __future__ import annotations

import random

ORDERED_OPS = ("=", "<>", ">", ">=", "<", "<=", "IN", "NOT IN", "BETWEEN")
NULL_OPS = ("IS NULL", "IS NOT NULL")
# a LIKE pattern is built only from a value holding none of these
LIKE_WILDCARDS = "%_\\"
# the magnitude under which a cross-type pivot is drawn (the module docstring)
CROSS_TYPE_BOUND = 10**12


def ordered_ops(ftype) -> tuple:
    """Every comparison operator, on every ordered type: the reference models this
    engine's order inside each type."""
    return ORDERED_OPS


def cross_type(rng: random.Random, ftype, v):
    """A pivot of the other numeric type: an INT column's value half a unit up or down (a
    double no stored integer equals), a FLOAT column's value as the integer nearest it;
    the value itself where it is null, a bool, or beyond `CROSS_TYPE_BOUND`."""
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)) or abs(v) >= CROSS_TYPE_BOUND:
        return v
    if isinstance(v, int):
        return v + rng.choice((-0.5, 0.5))
    return int(round(v))


def refine(rng: random.Random, ftype, v):
    return v
