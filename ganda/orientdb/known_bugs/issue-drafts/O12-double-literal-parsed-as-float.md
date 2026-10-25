# O12 -- a double literal is parsed as a float32: a scan misses the stored DOUBLE, an index lookup matches it

**Version:** OrientDB 3.2.56. Found while isolating a campaign mismatch (seed 211 of c60): `f2 IN [1.5, 776.417]` returned one record on a class scan and two through an index.

```sql
CREATE CLASS y1; CREATE PROPERTY y1.f2 DOUBLE;
INSERT INTO y1 SET f2 = 776.417;                 -- (a bind parameter; the stored value is the double 776.417)
SELECT FROM y1 WHERE f2 = 776.417;               -- []       the literal is a Float (776.4169921875)
SELECT FROM y1 WHERE f2 = 1.5;                   -- 1 record (1.5 is float32-exact)
SELECT FROM y1 WHERE f2 IN [1.5, 776.417];       -- 1 record
CREATE INDEX y1_f2 ON y1 (f2) NOTUNIQUE;
SELECT FROM y1 WHERE f2 = 776.417;               -- 1 record (the index converts the key to the property type)
SELECT FROM y1 WHERE f2 IN [1.5, 776.417];       -- 2 records
```

The same literal answers differently depending on whether the planner picks an index: the scan compares a `Float` with a `Double` (`776.4169921875 != 776.417`), the index lookup converts the key to `Double`. `SELECT FROM index:... WHERE key > 5.0` shows the same parse (`Float cannot be cast to Long`, O3). The fuzzer's `node_filter_ids` family spells every numeric pivot as a literal on a variant side (`<side>:literal`) and withholds a FLOAT pivot that is not float32-exact as this finding (`guards.o12_literal`).
