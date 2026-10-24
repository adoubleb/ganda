# O34 -- DECIMAL equality is scale-sensitive in DISTINCT, GROUP BY, `set()`, a scan's `IN (subquery)` and a string pivot (0.5 is not 0.50), but numeric in `=`, an index key, `max()` and ORDER BY

**Version:** OrientDB 3.2.56. Found while probing DECIMAL comparisons for the fuzzer's `<side>:rev` and `<side>:arith` variants.

```sql
CREATE CLASS n1; CREATE PROPERTY n1.id STRING; CREATE PROPERTY n1.f DECIMAL;
INSERT INTO n1 SET id = 'a', f = 0.5;           -- stored as the BigDecimal 0.5
INSERT INTO n1 SET id = 's', f = '0.50';        -- a string write keeps its scale: the BigDecimal 0.50 (reads back as 0.5 on the wire; f.asString() is '0.50')

SELECT id FROM n1 WHERE f = 0.5;                                  -- a, s     (compareTo)
SELECT max(f) FROM n1;                                            -- 0.5
SELECT DISTINCT f FROM n1;                                        -- 0.5, 0.5 (two rows)
SELECT f, count(*) FROM n1 GROUP BY f;                            -- two groups
SELECT set(f).size() FROM n1;                                     -- 2
SELECT id FROM (SELECT FROM n1) WHERE f IN (SELECT f FROM n1 WHERE id = 's');   -- s      (equals: 0.5 is not 0.50)
CREATE INDEX n1_f ON n1 (f) NOTUNIQUE;
SELECT id FROM n1 WHERE f IN (SELECT f FROM n1 WHERE id = 's');   -- a, s     (the index converts and compares numerically)
SELECT id FROM (SELECT FROM n1) WHERE f = '0.5';                  -- a        (a string pivot compares the decimal's STRING on a scan: '0.50' is not '0.5')
SELECT id FROM n1 WHERE f = '0.5';                                -- a, s     (the index converts the key)
```

`BigDecimal.equals` is scale-sensitive and `compareTo` is not; the engine uses one or the other depending on the path, so the same two stored values are equal under `=` and through an index and distinct under DISTINCT / GROUP BY / `set()` / a scan's `IN (subquery)`. The fuzzer's DECIMAL values are written from doubles (`BigDecimal.valueOf`, one canonical scale), so this is filed by the probe; a string pivot on a DECIMAL column is never drawn.
