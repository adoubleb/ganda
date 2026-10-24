# O3 -- `SELECT FROM index:<name> WHERE key > <int literal>` on a LONG index is a `ClassCastException`

**Version:** OrientDB 3.2.56.

```sql
CREATE CLASS n1; CREATE PROPERTY n1.f1 LONG; CREATE INDEX n1_f1 ON n1 (f1) NOTUNIQUE;
INSERT INTO n1 SET f1 = 6;
SELECT key, rid FROM index:n1_f1 WHERE key > 5;     -- 500 java.lang.ClassCastException: java.lang.Integer cannot be cast to java.lang.Long
SELECT key, rid FROM index:n1_f1 WHERE key > 5L;    -- ok
SELECT key, rid FROM index:n1_f1 WHERE key > 5.0;   -- 500 java.lang.ClassCastException: java.lang.Float cannot be cast to java.lang.Long
SELECT FROM n1 WHERE f1 > 5;                        -- ok (the class path converts the literal)
```

The direct index query does not coerce the literal to the index's key type, where the class query does. Same with a bind parameter holding an int. Minor, but a raw Java exception over HTTP; the fuzzer never generates `index:` queries because of it.
