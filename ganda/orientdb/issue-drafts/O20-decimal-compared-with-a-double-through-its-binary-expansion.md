# O20 -- a scan compares a DECIMAL property with a double parameter or literal through the double's exact binary expansion (`f = 0.1` matches nothing, `f >= 0.1` excludes the stored 0.1); an index lookup and a BETWEEN bound convert through the decimal string

**Version:** OrientDB 3.2.56. Found while probing the DECIMAL column type for the fuzzer's `narrow_type_probability` axis; confirmed on the class scan, in a bracket filter, in a MATCH `where:` and in an edge-class scan (`in.d = :p`), and reversed by an index on the column.

```sql
CREATE CLASS dc; CREATE PROPERTY dc.id STRING; CREATE PROPERTY dc.f DECIMAL;
INSERT INTO dc SET id = 'a', f = 0.1;   INSERT INTO dc SET id = 'b', f = 0.2;   INSERT INTO dc SET id = 'e', f = 0.5;
SELECT f.asString() FROM dc WHERE id = 'a';                 -- "0.1"   (stored through BigDecimal.valueOf(double))

SELECT id FROM dc WHERE f = 0.1;                            -- []      expected [a]
SELECT id FROM dc WHERE f = :p;         -- :p = 0.1            []
SELECT id FROM dc WHERE f >= :p;        -- :p = 0.1            [b, e]  expected [a, b, e]
SELECT id FROM dc WHERE f <> :p;        -- :p = 0.1            [a, b, e]
SELECT id FROM dc WHERE f IN :p;        -- :p = [0.1, 0.5]     [e]
SELECT id FROM dc WHERE f = :p;         -- :p = 0.5            [e]     (a dyadic double: exact in both)
SELECT id FROM dc WHERE f BETWEEN :a AND :b;   -- 0.1 .. 0.2   [a, b]  (the bounds go through the decimal string)
SELECT id FROM dc WHERE f >= :a AND f <= :b;   -- 0.1 .. 0.2   [b]

CREATE INDEX dc_f ON dc (f) NOTUNIQUE;
SELECT id FROM dc WHERE f = :p;         -- :p = 0.1            [a]
SELECT id FROM dc WHERE f >= :p;        -- :p = 0.1            [a, b, e]
SELECT id FROM dc WHERE f IN :p;        -- :p = [0.1, 0.5]     [a, e]
```

The value was written from the same double (the JSON `0.1`) and stored as the BigDecimal `0.1`; comparing it with that double again through `new BigDecimal(0.1)` (`0.1000000000000000055511151231257827...`) makes every non-dyadic decimal unreachable by equality on a scan and shifts every range by one ulp, while the index path -- and `BETWEEN`, and the write path -- use `BigDecimal.valueOf`. A value join (`f = $parent.$current.f`, a BigDecimal pivot) and `IN (SELECT f ...)` are exact. The fuzzer's reference models the scan; it withholds, per side, a `=` / `IN` / `<` / `<=` / `>` / `>=` with a non-dyadic double pivot on a DECIMAL column the side indexes (`guards.o20_predicate`), and never emits the `>= AND <=` rewrite of a DECIMAL `BETWEEN` with such a bound.
