"""Guards on one predicate, whichever IR asks it: the leaf shapes of the defects a
filter alone triggers (A25, A39, A40, A42).

**A25** (the legacy port's, found again by this port's first campaign with the rules
catalog, seed 1, and pinned by the probe fact A25): an OR whose disjuncts are all
whole-array equalities on one LIST attribute with two or more distinct values -- which
`replace-or-with-in` turns into `n.l IN [[..], [..]]` -- or such an IN written out, is
served from the ELEMENT keys of the expanded array index (`l[*]`) the data model holds on
that attribute, and answers nothing; the scan twins (`NOOPT`, `disableIndex`), a single
equality, a NOT over the OR and a one-element IN answer the rows. The guard withholds the
index-servable variants of the data models holding the index, at a select's filter, a
write's loop and a traversal's source or hop predicate alike (every one is a FILTER the
optimizer may serve).

**A42** (found by the large-scale campaign of 2026-10-24, seed 6, and pinned by the
probe fact A42): `LIKE` with a wildcard over a stored string of 125 000 code points or
more fails the query with errorNum 1543, "invalid regex value" -- the `%` is spelled
`(.|[\\r\\n])*`, a backtracking frame per character, and ICU's stack limit is reached
part-way through the value (at 199 999 code points under one wildcard, 124 999 under
two) -- where `==`, `CONTAINS`, `STARTS_WITH` and `LENGTH` answer. A `_` loops over
nothing and an exact pattern has no wildcard: neither fails. The guard withholds every
variant of a question whose predicate holds a LIKE with a `%` over a column where a
record the question reads stores `LIKE_WILDCARD_MAX_CHARS` code points or more (the
large scale's grown strings): the pulled-up forms too, which filter the same value.

**A40** (the first smoke's, retired 2026-10-25 and restored 2026-10-30 by the
symmetric-design campaign `sym-arangodb`; pinned by the probe fact A40): the optimizer
turns `NOT (@p IN f)` into `@p NOT IN f`, and both are false when `f` is null, absent or a
scalar, so the negation drops exactly those records. A plain `NOT (@p IN n.f)` in a
collection FILTER is rewritten on every data model alike; three placements are not:
- `[* FILTER NOT (p)]`, the doc data model's edge delete, disagrees with graph's and
  rel's `FILTER p` on the links whose `f` is not an array: a membership under no NOT in
  `p` is negated (and rewritten) on doc alone, one under a bare NOT is rewritten on graph
  and rel while doc's `NOT (NOT (...))` folds back to the membership;
- a NOT over an AND / OR that holds the membership is kept by a collection loop with
  nothing else to serve, but pushed through by `optimize-traversals` into a traversal's
  vertex or edge filter;
- the same NOT is pushed through by `use-indexes` where a persistent index serves another
  field of the filter -- a collection loop and a pulled-up source filter (`FOR hr IN (...)
  FILTER NOT ((@p IN hr.f OR hr.g != @q))`) once its subquery is inlined, which happens on
  one data model's form and not another's (campaign seeds 10168, 11869: rel's alone),
  while the base forms over one collection and one index plan alike.
`negated_memberships` and `complement_memberships` find the leaves; the IR guards judge
the placement.

**A39** (the legacy port's, met again by `sym-arangodb` seed 11918; pinned by the probe
fact A39): `use-indexes` merges an equality, a range bound or a `!=` on one attribute as
if an INTEGER constant and a double one or two ulps from it were the same number, and
plans the conjunction as empty (`n.f > -5e-324 AND n.f == 0`, `n.f != 1.0000000000000002
AND n.f == 1`), with no index on the collection; the executor compares exactly. The
guard withholds every variant of a question whose predicate pairs such constants on one
field where a record the question reads stores a number between them.
"""
from __future__ import annotations

import struct

from ganda.core.query.ir.predicates import Compound, Predicate, leaves
from ganda.arangodb.rules import works_around

A25 = "A25"
works_around("A25")
A39 = "A39"
works_around("A39")
A40 = "A40"
works_around("A40")
A42 = "A42"
works_around("A42")
# the measured first failing lengths, 124 999 code points under two wildcards and
# 199 999 under one, rounded down (Ganda's patterns hold at most two)
LIKE_WILDCARD_MAX_CHARS = 100_000


# --- A42 ------------------------------------------------------------------------------------------


def like_wildcard_fields(p) -> set:
    """The fields a LIKE leaf tests with a `%` wildcard, anywhere in the tree (a `_`
    loops over nothing, an exact pattern has no wildcard)."""
    if p is None:
        return set()
    if isinstance(p, Compound):
        out: set = set()
        for c in p.children:
            out |= like_wildcard_fields(c)
        return out
    if isinstance(p, Predicate) and p.op == "LIKE" and isinstance(p.constant, str) and "%" in p.constant:
        return {p.field}
    return set()


def _has_long_string(rows, fields: set) -> bool:
    return any(isinstance(v, str) and len(v) >= LIKE_WILDCARD_MAX_CHARS for r in rows for f in fields for v in (r.values.get(f),))


def a42_shape(p, rows) -> bool:
    fields = like_wildcard_fields(p)
    return bool(fields) and _has_long_string(rows, fields)


# --- A40 ------------------------------------------------------------------------------------------


def _memberships(p, nots: int = 0, spanned: bool = False):
    """(leaf, NOTs above it, whether an AND / OR lies between it and the nearest NOT)
    for every `@p IN f` leaf (`ANY ==`, the reversed form, and `ANY IN` / `ALL IN` are
    never rewritten)."""
    if isinstance(p, Compound):
        for c in p.children:
            yield from _memberships(c, nots + 1, False) if p.op == "NOT" else _memberships(c, nots, spanned or nots > 0)
    elif isinstance(p, Predicate) and p.op == "HAS" and not p.rev and p.sub is None and p.arith is None:
        yield p, nots, spanned


def negated_memberships(p) -> list:
    """The membership leaves under an odd number of NOTs with an AND / OR between the
    leaf and its nearest NOT: a NOT a collection loop keeps, a traversal and an inlined
    pulled-up filter push through."""
    return [lf for lf, nots, spanned in _memberships(p) if nots % 2 and spanned]


def complement_memberships(p) -> list:
    """The membership leaves on which `p` and the complement `NOT (p)` meet the rewrite
    differently: all but those under a NOT over an AND / OR, which neither rewrites."""
    return [lf for lf, nots, spanned in _memberships(p) if not (nots % 2 and spanned)]


def _value(values: dict, lf):
    v = values.get(lf.field)
    if lf.key is not None:
        v = v.get(lf.key) if isinstance(v, dict) else None
    return v


def holds_non_array(rows, lfs) -> bool:
    """Whether a record stores a null, nothing or a scalar where a leaf tests membership."""
    return any(not isinstance(_value(r.values, lf), list) for r in rows for lf in lfs)


# --- A39 ------------------------------------------------------------------------------------------

_A39_OPS = ("=", "!=", "<", "<=", ">", ">=", "IN", "NOT IN", "BETWEEN")


def _ordinal(x: float) -> int:
    """A double's position in the ordered sequence of doubles (adjacent doubles differ by 1)."""
    n = struct.unpack("<q", struct.pack("<d", x))[0]
    return n if n >= 0 else -(n & 0x7FFF_FFFF_FFFF_FFFF)


def _numbers(constant) -> list:
    vals = constant if isinstance(constant, (list, tuple)) else (constant,)
    return [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]


def a39_shape(p, rows) -> bool:
    """An INTEGER constant and a double one or two ulps from it compared with one field
    (two ulps still fire, the probe's `-1e-323` against `0`), and a record storing a
    number between the two."""
    consts: dict = {}
    for lf in leaves(p):
        if lf.op in _A39_OPS and lf.sub is None and lf.arith is None and not lf.literal:
            consts.setdefault((lf.field, lf.key), []).extend(_numbers(lf.constant))
    for (field, key), vals in consts.items():
        ints = {v for v in vals if isinstance(v, int)}
        doubles = {v for v in vals if isinstance(v, float)}
        pairs = [(i, d) for i in ints for d in doubles if abs(i) < 2**63 and 1 <= abs(_ordinal(float(i)) - _ordinal(d)) <= 2]
        for i, d in pairs:
            lo, hi = min(i, d), max(i, d)
            for r in rows:
                v = r.values.get(field)
                if key is not None:
                    v = v.get(key) if isinstance(v, dict) else None
                if isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi:
                    return True
    return False


# --- A25 ------------------------------------------------------------------------------------------


def or_of_list_equalities(p, under_or: bool = False) -> set:
    """The LIST fields some OR compares for equality with two or more distinct array
    values, every disjunct on that one field (the shape `replace-or-with-in` rewrites),
    plus those an IN of two or more arrays names. An OR under a NOT and an OR that is a
    disjunct of a wider OR are left alone: the optimizer flattens the latter and finds
    the mixed attributes, and never rewrites the former."""
    if isinstance(p, Predicate):
        if p.op == "IN" and p.ftype == "list" and isinstance(p.constant, list) and len({repr(v) for v in p.constant}) >= 2:
            return {p.field}
        return set()
    if not isinstance(p, Compound) or p.op == "NOT":
        return set()
    out: set = set()
    if p.op == "OR" and not under_or:
        leaves = [c for c in p.children if isinstance(c, Predicate) and c.op == "=" and c.ftype == "list" and c.sub is None and c.key is None]
        if len(leaves) == len(p.children) and len({c.field for c in leaves}) == 1 and len({repr(c.constant) for c in leaves}) >= 2:
            out.add(leaves[0].field)
    for c in p.children:
        out |= or_of_list_equalities(c, under_or=p.op == "OR")
    return out


def array_indexed(schema, table: str, field: str, data_model: str, edge: bool = False) -> bool:
    """Whether `data_model`'s database holds the expanded array index over `field` of
    `table` (every index on a LIST field is one, `concrete_syntax/setup/common._index_fields`)."""
    if edge:
        return any(ix.target == "edge" and ix.table == table and ix.field == field and ix.applies_to(data_model) for ix in schema.indexes)
    try:
        return bool(schema.node_indexes(table, field, data_model))
    except KeyError:
        return False


def a25_shape(p, schema, table: str, data_model: str, edge: bool = False) -> bool:
    return any(array_indexed(schema, table, f, data_model, edge) for f in or_of_list_equalities(p))
