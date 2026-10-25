# O4 -- the planner answers an equality / IN predicate through a DICTIONARY index, dropping every record but one per key

**Version:** OrientDB 3.2.56.

```sql
CREATE CLASS k1; CREATE PROPERTY k1.f STRING; CREATE INDEX k1_f ON k1 (f) DICTIONARY;
INSERT INTO k1 SET f = 'dup'; INSERT INTO k1 SET f = 'dup'; INSERT INTO k1 SET f = 'dup';
SELECT FROM k1 WHERE f = 'dup';          -- 1 record (EXPLAIN: FETCH FROM INDEX k1_f)
SELECT count(*) FROM k1 WHERE f = 'dup'; -- 1
SELECT FROM k1 WHERE f IN ['dup'];       -- 1 record
SELECT FROM k1 WHERE f > 'a';            -- 3 records (a range is not served by the index)
```

A DICTIONARY index is documented to keep the last record per key; that is a property of the *index*. Using it as the access path for a *filter* changes the query's answer: a `WHERE f = 'dup'` must return every record with that value whatever indexes exist. The fuzzer's generator demotes a DICTIONARY index over duplicate values to NOTUNIQUE (`generator._demote_colliding_indexes`) so the campaign does not re-report this on every instance; `--keep-dictionary-duplicates` reproduces it through every identical-text node family (`guards.o4_shape` names the shape).

## Report status
Reported as 10880. Confirmed and fixed.