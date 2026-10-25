# O23 -- an `ORDER BY` walked from a composite index that keeps nulls puts the records with a null key FIRST ascending and LAST descending, the reverse of a sort and of a single-field index walk

**Version:** OrientDB 3.2.56. Found while probing multi-key ordering for the fuzzer's `node_ordered_keys` family, the sibling of issue O18 (an index that IGNORES nulls drops those records instead).

```sql
CREATE CLASS ob; CREATE PROPERTY ob.id STRING; CREATE PROPERTY ob.f LONG; CREATE PROPERTY ob.g LONG;
INSERT INTO ob SET id = 'r0', f = 1, g = 1;   INSERT INTO ob SET id = 'r1', f = 1;   INSERT INTO ob SET id = 'r2', f = 1, g = 0;
INSERT INTO ob SET id = 'r3', g = 5;          INSERT INTO ob SET id = 'r4', f = 2;   INSERT INTO ob SET id = 'r5';
INSERT INTO ob SET id = 'r6', f = 2, g = 2;

SELECT id FROM ob ORDER BY f ASC, g ASC, id ASC;    -- [r2, r0, r1, r6, r4, r3, r5]   (a sort: nulls last, per key)
SELECT id FROM ob ORDER BY f DESC, g DESC, id ASC;  -- [r5, r3, r4, r6, r1, r0, r2]   (nulls first, per key)

CREATE INDEX ob_fg ON ob (f, g) NOTUNIQUE METADATA {ignoreNullValues: false};
SELECT id FROM ob ORDER BY f ASC, g ASC;            -- [r5, r3, r1, r2, r0, r4, r6]   expected [r2, r0, r1, r6, r4, r3, r5]
SELECT id FROM ob ORDER BY f DESC, g DESC;          -- [r6, r4, r0, r2, r1, r3, r5]   expected [r5, r3, r4, r6, r1, r0, r2]
SELECT id FROM ob ORDER BY f ASC;                   -- [r5, r3, r1, r2, r0, r4, r6]   (a prefix of the index: the same walk)
SELECT id FROM ob ORDER BY f ASC, g DESC;           -- [r1, r0, r2, r4, r6, r5, r3]   (mixed directions: a sort, correct)

DROP INDEX ob_fg; CREATE INDEX ob_fg ON ob (f, g) NOTUNIQUE METADATA {ignoreNullValues: true};
SELECT id FROM ob ORDER BY f ASC, g ASC;            -- [r2, r0, r6]                   (O18's face: the null-keyed records are gone)
SELECT id FROM ob ORDER BY f DESC, g DESC;          -- []

DROP INDEX ob_fg; CREATE INDEX ob_f ON ob (f) NOTUNIQUE METADATA {ignoreNullValues: false};
SELECT id FROM ob ORDER BY f ASC;                   -- [r0, r1, r2, r4, r6, r3, r5]   (a single-field index that keeps nulls: correct)
SELECT id FROM ob ORDER BY f DESC;                  -- [r3, r5, r6, r4, r2, r1, r0]
```

`EXPLAIN` shows `FetchFromIndexValuesStep` for the composite walk: the composite key sorts a null component lowest, and the walk is emitted as the order without moving the null keys to where a sort puts them. The fuzzer withholds, per side, a `node_ordered_keys` case whose keys are a one-direction prefix of a composite index that keeps nulls on a class holding a null in a key column (`guards.ordered_keys_known`, "O23"), and one whose keys are such a prefix of any range index that ignores nulls ("O18").
