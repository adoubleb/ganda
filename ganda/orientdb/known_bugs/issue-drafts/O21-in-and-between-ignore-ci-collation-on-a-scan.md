# O21 -- `IN` and `BETWEEN` on a `COLLATE ci` property compare code units on a scan (`f IN ['A']` misses `a`) where `=` and `<` compare case-insensitively and an index answers both case-insensitively

**Version:** OrientDB 3.2.56. Found while probing case-insensitive collation for the fuzzer's `collate_probability` axis.

```sql
CREATE CLASS cv; CREATE PROPERTY cv.id STRING; CREATE PROPERTY cv.s STRING; ALTER PROPERTY cv.s COLLATE ci;
INSERT INTO cv SET id = 'r0', s = 'a';   INSERT INTO cv SET id = 'r1', s = 'A';   INSERT INTO cv SET id = 'r2', s = 'b';
INSERT INTO cv SET id = 'r3', s = 'ab';  INSERT INTO cv SET id = 'r4', s = 'AB';

SELECT id FROM cv WHERE s = 'A';                         -- [r0, r1]          (collated)
SELECT id FROM cv WHERE s < 'B';                         -- [r0, r1, r3, r4]  (collated)
SELECT id FROM cv WHERE s IN ['A'];                      -- [r1]              expected [r0, r1]
SELECT id FROM cv WHERE s IN ['A', 'b'];                 -- [r1, r2]
SELECT id FROM cv WHERE s BETWEEN 'A' AND 'B';           -- [r1, r4]          expected [r0, r1, r2, r3, r4] ('a'..'b' collated)
SELECT id FROM cv WHERE s IN (SELECT s FROM cv WHERE id = 'r1');   -- [r1]

CREATE INDEX cv_s ON cv (s) NOTUNIQUE;
SELECT id FROM cv WHERE s IN ['A'];                      -- [r0, r1]
SELECT id FROM cv WHERE s BETWEEN 'A' AND 'B';           -- [r0, r1, r2, r3, r4]
SELECT id FROM cv WHERE s IN (SELECT s FROM cv WHERE id = 'r1');   -- [r0, r1]
```

A NOTUNIQUE_HASH_INDEX shows the `IN` face only (it answers a `BETWEEN` by a scan). `GROUP BY`, `DISTINCT`, `max()` and `min()` do not collate either (case variants stay apart), which may be intended; `IN` disagreeing with `=` on one property, and the answer flipping with an index, is not. The fuzzer's reference models the scan (`IN` / `BETWEEN` exact, `=` / `<>` / `<` .. `>=` lower-cased) and withholds, per side, an `IN` / `BETWEEN` leaf on a ci column the side indexes (`BETWEEN`: a range-capable kind) and `node_in_subquery` over a ci column on an indexed side (`guards.o21_predicate`).


**Composite face (probed 3.2.56):** `b = true AND s IN ['x']` through a composite index on `(b, s)` answers case-insensitively (`x` and `X`) where the scan answers `x` only.
