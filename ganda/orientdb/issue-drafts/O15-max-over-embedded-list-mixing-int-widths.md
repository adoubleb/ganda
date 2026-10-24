# O15 -- `max()` / `min()` over an embedded list whose numbers mix Integer and Long is a `ClassCastException`

**Version:** OrientDB 3.2.56. Found by the fuzzer's `emax` / `emin` projections (the last hop's relationship attribute) on their first run (seeds 3, 5 and 6 of the smoke campaign): the doc side, whose relationship attributes live in embedded documents, answered `java.lang.ClassCastException: java.lang.Integer cannot be cast to java.lang.Long` (or the reverse) where the graph E class and the junction class, whose attribute is a declared LONG, aggregated fine.

```sql
CREATE CLASS m1; CREATE PROPERTY m1.e1 EMBEDDEDLIST;
INSERT INTO m1 SET e1 = [{w: 3}, {w: 1099511627776}];      -- an Integer and a Long, as the JSON parser types them
SELECT max(e1.w) FROM m1;                                 -- HTTP 500: java.lang.ClassCastException: java.lang.Long cannot be cast to java.lang.Integer
SELECT min(e1.w) FROM m1;                                 -- the same
SELECT sum(e1.w) FROM m1;                                 -- 1099511627779 (fine)
SELECT max(e1[w < 100].w) FROM m1;                        -- 3 (one width: fine)
```

An undeclared embedded value keeps the width the parser gave it, and `max`/`min` compare the elements with `Comparable.compareTo` across the two boxed types. The same list projects fine (`SELECT e1.w`), so the multiset projection `efield` stays admitted; the fuzzer withholds the doc side's `emax`/`emin` (every doc spelling) when the edge attribute's stored integers do not share one width (`guards.o15_shape`).
