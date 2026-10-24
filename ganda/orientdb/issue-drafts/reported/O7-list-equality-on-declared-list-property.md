# Collection operators on a declared `EMBEDDEDLIST LONG` never match integer literals (Integer vs Long boxes), and an index silently changes the answer

**Version:** OrientDB 3.2.56 (`orientdb:latest`), new SQL executor, REST `/command` (same results through bind parameters).

## Reproduction

```sql
CREATE CLASS L;
CREATE PROPERTY L.f EMBEDDEDLIST LONG;
INSERT INTO L SET id = 'a', f = [1, 2];
INSERT INTO L SET id = 'b', f = [1, 1];
INSERT INTO L SET id = 'c', f = [7];

SELECT id FROM L WHERE f = [1, 2];               -- []        expected [a]
SELECT id FROM L WHERE f IN [[1, 2]];            -- []        expected [a]
SELECT id FROM L WHERE f CONTAINSANY [1, 7];     -- []        expected [a, b, c]
SELECT id FROM L WHERE f CONTAINSALL [1, 2];     -- []        expected [a]
SELECT id FROM L WHERE f <> [1, 2];              -- [a, b, c] expected [b, c]

SELECT id FROM L WHERE f = [1L, 2L];             -- [a]       Long elements: works
SELECT id FROM L WHERE f CONTAINS 2;             -- [a]       scalar element: coerced, works

CREATE INDEX L_f ON L (f) NOTUNIQUE METADATA {ignoreNullValues: true};

SELECT id FROM L WHERE f = [1, 2];               -- [a, a, b] expected [a]; index-served, one row per matching ELEMENT
SELECT id FROM L WHERE f CONTAINSANY [1, 7];     -- [a, b, c] correct now (index-served)
SELECT id FROM L WHERE f CONTAINSALL [1, 2];     -- []        still a scan, still wrong
```

## What is wrong

1. **A literal list is never coerced to the declared element type.** The stored
   elements are `Long`; the literal's are `Integer`; the list comparison uses box
   equality, so `=`, `IN`, `CONTAINSANY` and `CONTAINSALL` match nothing and `<>`
   matches everything. A scalar comparison on the same class coerces (`g = 5` on a
   `LONG g` works, and so does `f CONTAINS 2` element-wise), so the collection
   operators are inconsistent with the scalar ones and with the declared schema.
   The only ways to hit a record are to write the literal as `[1L, 2L]` or to avoid
   the schema: the same `[1, 2]` stored in an untyped embedded document matches.

2. **Adding an index changes the result set.** With a `NOTUNIQUE` index on `f`,
   `CONTAINSANY` becomes correct and `f = [1, 2]` stops answering nothing and
   answers **element-wise with duplicates**: `a` twice (elements 1 and 2) and `b`
   (element 1, `[1, 1]` is not `[1, 2]`). The same query text answers `[]`, or
   `[a, a, b]`, depending only on whether an index exists. Neither is `[a]`.

The same box mismatch appears on `EMBEDDEDMAP LONG`: `m CONTAINSVALUE 2` matches
nothing on a scan and the right records through a `BY VALUE` index.

## Expected

Collection operators should convert the operand's elements to the property's
declared linked type before comparing, as the scalar path already does; `f = [1, 2]`
should answer `[a]` with or without an index.

## Report Status
Reported as 10882. Confirmed as bug.