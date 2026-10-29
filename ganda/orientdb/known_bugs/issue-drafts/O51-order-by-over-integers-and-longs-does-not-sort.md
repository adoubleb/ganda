# ORDER BY over a field holding both INTEGER and LONG values does not sort

**OrientDB version:** 3.2.57 (official Docker image `orientdb:3.2.57`). The code involved
(`OOrderByItem.compare`) is the same on `develop` (commit `e99f8f0f4e`, 2026-10-16), so the fix
is needed there too.
**Client:** REST, `POST /command/<db>/sql` (the new SQL executor).

## Steps to reproduce

```sql
CREATE CLASS qy;                         -- no property declared: a JSON integer is an INTEGER when it fits, a LONG otherwise
INSERT INTO qy SET id = 'a', w = 1;
INSERT INTO qy SET id = 'c', w = 1099511627776;          -- 2^40, a LONG

SELECT id FROM qy ORDER BY w ASC;        -- a, c
SELECT id FROM qy ORDER BY w DESC;       -- a, c   (Wrong: expected c, a)

INSERT INTO qy SET id = 'z', w = -3;
SELECT id FROM qy ORDER BY w ASC;        -- a, c, z   (Wrong: expected z, a, c)
```

Five values, `1, 2, 2^40, -3, 5` inserted in that order (ids `a b c d e`):

```sql
SELECT id FROM qu ORDER BY w ASC;        -- a, b, c, d, e   (expected d, a, b, e, c)
SELECT id FROM qu ORDER BY w DESC;       -- b, a, c, e, d   (expected c, e, b, a, d)
SELECT id FROM (SELECT FROM qu) ORDER BY w ASC;            -- the same order as the first
SELECT id, w.asLong() AS x FROM qu ORDER BY x ASC;         -- d, a, b, e, c   (right)
SELECT id FROM qu WHERE w < 1099511627776;                 -- a, b, d, e      (right: comparisons widen)
SELECT max(w), min(w) FROM qu;                             -- 2^40, -3        (right)
```

With the property declared (`CREATE PROPERTY qt.w LONG`) every value is stored as a LONG and
both orders are right. The same holds for the attributes of embedded documents, which carry no
schema: `SELECT l.eid FROM (SELECT e1 AS l FROM src UNWIND l) ORDER BY l.w` misorders as soon as
one link's `w` needs 64 bits.

## Expected behavior

`ORDER BY` orders numbers by value, whatever their Java width, as `<`, `max()` and `min()` do
on the same values.

## Actual behavior

Rows whose values mix INTEGER and LONG come back in no particular order: a pair of an INTEGER
and a LONG is kept as it was read, in both directions, and since two INTEGERs (or two LONGs)
are still ordered, the sort's comparisons contradict each other and the result depends on the
input order. No error reaches the client.

## Analysis

`OOrderByItem.compare(OResult, OResult, OCommandContext)`, for two non-null values that are not
both strings:

```java
} else if (aVal instanceof Comparable && bVal instanceof Comparable) {
  try {
    result = ((Comparable) aVal).compareTo(bVal);
  } catch (Exception e) {
    logger.error("Error during comparision", e);
    result = 0;
  }
}
```

`Integer.compareTo(Long)` throws `ClassCastException`, which is caught and turned into `0`:
the two values are reported EQUAL. The comparator is then not transitive (1 < 5 and -3 < 5, but
1 = 2^40 = -3), and `OrderByStep`'s `List.sort` (TimSort) answers whatever its merge order
produces (the server logs one `Error during comparision` per such comparison). The WHERE path's
`OLtOperator` / `OGtOperator` widen two numbers with `OType.castComparableNumber` before
`compareTo`, which is why the filter is right.

A fix: compare two `Number`s through `OType.castComparableNumber` (or widen both to the wider
type) before `compareTo`, as the comparison operators do; and not read an exception as
equality.

## Scope

Any `ORDER BY` key whose values mix numeric widths: a field no property declares (a schemaless
class, a class with the field undeclared, the attributes of an embedded document or map), and
an expression whose results mix widths. A declared numeric property stores one width and is
not affected. The same root -- `compareTo` across boxed widths -- is O15 (`max()` / `min()`
over an embedded list mixing widths throw `ClassCastException`); the ORDER BY face is silent.

## How Ganda met it

Exploring the frame grid's remaining hole (2026-10-28): the doc data model answers an
edge-type select only for `ids` and `count`, through its link lists; asking every select kind
over the links unwound into rows (`FROM (SELECT e1 AS l FROM src UNWIND l)`) beside the graph
data model's E class and the rel data model's junction class answered alike on filters,
aggregates, groups and distinct values, and differently on `page` / `keys` ordered by an
attribute holding a 2^40 among small integers. The graph and rel data models declare the
attribute LONG, the doc data model's embedded documents do not. Ganda does not ask such a select
of the doc data model yet, so no campaign meets it and no guard reads it; once it does, the
guard withholds the doc data model's ordered kinds where the attribute's stored integers do not
share one width, as O15's does for `emax` / `emin`.
