# O11 -- `f < p` served by a composite index led by `f` returns the records whose `f` is null

**Version:** OrientDB 3.2.56. Found by the fuzzer's `node_filter_ids` family as a cross-model mismatch (seeds 110 and 111 of the first 40-seed campaign): the side holding a composite index `(f1, f2)` on the class returned two extra records for `f1 < :p`, both with a null or absent `f1`; the two other sides (a scan, and a single-field index) and the reference agreed on the six correct records.

```sql
CREATE CLASS x1; CREATE PROPERTY x1.f1 LONG; CREATE PROPERTY x1.f2 STRING;
INSERT INTO x1 SET f1 = 1, f2 = 'a';
INSERT INTO x1 SET f1 = 9223372036854775807, f2 = 'b';
INSERT INTO x1 SET f2 = 'c';                            -- f1 absent
SELECT FROM x1 WHERE f1 < 5;                            -- 1 record (scan)
CREATE INDEX x1_f1 ON x1 (f1) NOTUNIQUE;
SELECT FROM x1 WHERE f1 < 5;                            -- 1 record (single-field index)
DROP INDEX x1_f1;
CREATE INDEX x1_f1f2 ON x1 (f1, f2) NOTUNIQUE;
SELECT FROM x1 WHERE f1 < 5;                            -- 2 records: the f1-less record is returned
```

`<` and `<=` are affected (nulls sort lowest in the index and the composite range starts from the bottom); `>`, `>=`, `=`, `BETWEEN` are not. The fuzzer withholds `<`/`<=` on the lead column of a composite index on the side holding it when the class has a null in that column (`guards.o11_predicate`).
