# O33 -- `count(distinct(f))` counts every non-null row; `sum(distinct(f))` sums every row: an aggregate over `distinct()` ignores the distinct

**Version:** OrientDB 3.2.56. Found while probing the aggregate functions for the fuzzer's `node_agg` family.

```sql
CREATE CLASS n1; CREATE PROPERTY n1.f LONG;
INSERT INTO n1 SET f = 1; INSERT INTO n1 SET f = 1; INSERT INTO n1 SET f = 2; INSERT INTO n1 SET f = 5; INSERT INTO n1 SET f = 5; INSERT INTO n1;

SELECT distinct(f) FROM n1;                                       -- 1, 2, 5, null   (one row per distinct value, the null included)
SELECT count(distinct(f)) FROM n1;                                -- 5               (expected 3: the non-null rows are counted)
SELECT sum(distinct(f)) FROM n1;                                  -- 14              (expected 8)
SELECT count(*) FROM (SELECT DISTINCT f FROM n1 WHERE f IS NOT NULL);   -- 3
SELECT set(f).size() FROM n1;                                     -- 3
SELECT count(DISTINCT f) FROM n1;                                 -- parse error (the SQL-standard spelling is not accepted)
```

`distinct()` is a row-filtering projection function; nested inside another aggregate it is evaluated per row and the outer aggregate sees every value. The query is accepted without complaint and returns a plausible number. The fuzzer asks the distinct count three ways (`node_agg`'s `distinct_count`: a DISTINCT subquery counted, `set(f).size()`, and `count(distinct(f))` withheld on every side as O33).

## Report Status
Reported as 10887 and confirmed. 
