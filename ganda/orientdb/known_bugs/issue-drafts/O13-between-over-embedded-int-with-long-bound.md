# O13 -- `BETWEEN` with a Long bound never matches an embedded Integer value

**Version:** OrientDB 3.2.56. Found by the fuzzer as a cross-model mismatch (seed 245 of c60): the doc side's `e1[f2 BETWEEN :a AND :b]` over embedded link documents missed a link whose `f2` is 946 with bounds `946` and `123456789012345678`; the graph and relational sides, whose `f2` is a declared LONG, and the reference agreed it matches.

```sql
CREATE CLASS z1; CREATE PROPERTY z1.e1 EMBEDDEDLIST; CREATE PROPERTY z1.f2 LONG;
INSERT INTO z1 SET f2 = 946, e1 = [{f2: 946, t: 'x'}, {f2: 123456789012345678, t: 'y'}];
SELECT e1[f2 BETWEEN 946 AND 123456789012345678].t FROM z1;   -- [y]      expected [x, y]
SELECT e1[f2 >= 946 AND f2 <= 123456789012345678].t FROM z1;  -- [x, y]
SELECT e1[f2 BETWEEN 946 AND 1000].t FROM z1;                 -- [x]      (both bounds Integer)
SELECT e1[f2 BETWEEN 946 AND 2147483648].t FROM z1;           -- []       (the upper bound is the first Long)
SELECT FROM z1 WHERE f2 BETWEEN 946 AND 123456789012345678;   -- 1 record (declared LONG: fine)
```

An embedded value with no declared type is stored as the Integer the JSON parser produced; `BETWEEN` compares it against a Long bound as a different type and fails where `>=`/`<=` coerce. The fuzzer withholds the doc side for an edge BETWEEN whose bounds and stored values do not share one int width (`guards.o13_shape`).
