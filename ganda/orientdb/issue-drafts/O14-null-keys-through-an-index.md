# O14 -- a null key through an index: `f IN (SELECT ...)` with a null in the subquery's answer returns the null records; `f = :p` with a null parameter does too; `f IN [x, null]` is a NullPointerException

**Version:** OrientDB 3.2.56. Found by the fuzzer's `node_in_subquery` family on its first run (seeds 4, 5 and 6 of the smoke campaign): the sides holding an index on the filtered column (NOTUNIQUE, NOTUNIQUE_HASH_INDEX with and without `ignoreNullValues`) returned the records whose column is null or absent, the scanning sides did not; on seed 5 every side held the index and only the Python reference disagreed.

```sql
CREATE CLASS n1; CREATE PROPERTY n1.id STRING; CREATE PROPERTY n1.f1 LONG;
INSERT INTO n1 SET id = 'a', f1 = 1;
INSERT INTO n1 SET id = 'b', f1 = 5;
INSERT INTO n1 SET id = 'e', f1 = null;                    -- explicit null
INSERT INTO n1 SET id = 'h';                               -- absent

SELECT id FROM n1 WHERE f1 IN (SELECT f1 FROM n1 WHERE id IN ['a', 'e']);   -- [a]          (class scan)
SELECT id FROM n1 WHERE f1 = :p;                            -- []  with p = null
SELECT id FROM n1 WHERE f1 IN [1, null];                    -- [a]

CREATE INDEX n1_f1 ON n1 (f1) NOTUNIQUE;                    -- any kind, with or without ignoreNullValues
SELECT id FROM n1 WHERE f1 IN (SELECT f1 FROM n1 WHERE id IN ['a', 'e']);   -- [a, e, h]    the null records
SELECT id FROM n1 WHERE f1 = :p;                            -- [e, h]  with p = null
SELECT id FROM n1 WHERE f1 > :p;                            -- [e, h]  with p = null (a scan: [])
SELECT id FROM n1 WHERE f1 IN [1, null];                    -- HTTP 500: java.lang.NullPointerException
```

A class scan follows the null rules (`null = null` is false, a null in an `IN` list matches nothing); the index lookup treats the null key as a value and answers the records under it, or throws when the list mixes a value with a null. The typical form is the semi-join `WHERE f IN (SELECT g FROM t2)` the moment one `t2` record lacks `g`. The fuzzer withholds the `node_in_subquery` family on a side that indexes the filtered column when the subquery's answer holds a null (`guards.o14_shape`); the parameter forms are pinned in `scripts/probe_osql.py`.
