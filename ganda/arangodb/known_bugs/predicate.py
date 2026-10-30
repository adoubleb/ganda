"""Guards on one predicate, whichever IR asks it: the leaf shapes of the defects a
filter alone triggers (A25, A42).

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
"""
from __future__ import annotations

from ganda.core.query.ir.predicates import Compound, Predicate
from ganda.arangodb.rules import works_around

A25 = "A25"
works_around("A25")
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
