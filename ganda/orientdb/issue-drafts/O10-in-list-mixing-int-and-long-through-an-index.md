# O10 -- `f IN [<int>, <long>]` served by an index is a `ClassCastException`

**Version:** OrientDB 3.2.56. Found by the fuzzer's `node_filter_count` family as an error asymmetry (seed 104 of the first 40-seed campaign): the side holding an index on the column answered HTTP 500, the two scanning sides answered the count.

```sql
CREATE CLASS x1; CREATE PROPERTY x1.f1 LONG;
INSERT INTO x1 SET f1 = 1; INSERT INTO x1 SET f1 = 9223372036854775807;
SELECT FROM x1 WHERE f1 IN [1, 9223372036854775807];    -- 2 records (class scan)
CREATE INDEX x1_f1 ON x1 (f1) NOTUNIQUE;                 -- or UNIQUE, NOTUNIQUE_HASH_INDEX, with or without ignoreNullValues
SELECT FROM x1 WHERE f1 IN [1, 9223372036854775807];    -- HTTP 500: java.lang.ClassCastException
SELECT FROM x1 WHERE f1 IN [1, 5];                       -- fine (all Integer)
SELECT FROM x1 WHERE f1 = 9223372036854775807;           -- fine
```

The list literal (or bind parameter) parses small values as `Integer` and wide ones as `Long`; the index lookup for `IN` casts the elements to one type and throws when they differ. A scan compares numerically. The fuzzer withholds the shape per side where the column is indexed (`guards.o10_predicate`).

The same cast fails between `Double` and `Integer` on a DOUBLE column (found by the cross-type pivots, seed 408 of campaign c60b, every index kind including DICTIONARY):

```sql
CREATE CLASS x2; CREATE PROPERTY x2.f2 DOUBLE;
INSERT INTO x2 SET f2 = 1.5; INSERT INTO x2 SET f2 = 2.0;
SELECT FROM x2 WHERE f2 IN [1.5, 2];                     -- 2 records (class scan)
CREATE INDEX x2_f2 ON x2 (f2) NOTUNIQUE;
SELECT FROM x2 WHERE f2 IN [1.5, 2];                     -- HTTP 500: java.lang.ClassCastException: java.lang.Double cannot be cast to java.lang.Integer
```

The guard's shape is therefore an IN list holding numbers of two boxed types (Integer, Long, Double) on an indexed column.


**Further faces (probed 3.2.56):** the same `ClassCastException` for `f IN [0.125, 0]` (Double and Integer) through an index on a FLOAT column, for `f IN [0.1, 5]` through an index on a DECIMAL column, and for `f IN [5, 2147483648]` (Integer and Long) through an index on an INTEGER column.


**Composite face (campaign c100, seed 614; probed):** the same `ClassCastException` when the mixed-box `IN` sits on the SECOND field of a composite index next to an equality on its lead (`b = true AND f IN [7, 420, -4611686018427387904]` through a NOTUNIQUE_HASH_INDEX on `(b, f)`), on every side holding such an index.
