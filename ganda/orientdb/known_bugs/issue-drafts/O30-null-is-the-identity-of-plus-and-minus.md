# O30 -- a null operand is the identity of `+` and `-` (`null + 1` is 1) but absorbs `*`, `/` and `%`

**Version:** OrientDB 3.2.56. Found while probing arithmetic over the field for the fuzzer's `<side>:arith` predicate spelling.

```sql
CREATE CLASS n1; CREATE PROPERTY n1.id STRING; CREATE PROPERTY n1.f LONG; CREATE PROPERTY n1.s STRING; CREATE PROPERTY n1.t DATETIME;
INSERT INTO n1 SET id = 'a', f = 5, s = 'x', t = '2024-01-01 00:00:00';
INSERT INTO n1 SET id = 'n';                  -- f, s, t absent

SELECT f + 1 FROM n1 WHERE id = 'n';          -- 1        (expected null)
SELECT f - 2 FROM n1 WHERE id = 'n';          -- -2
SELECT :p + f FROM n1 WHERE id = 'n';         -- :p = 7 -> 7
SELECT f + 1.5 FROM n1 WHERE id = 'n';        -- 1.5
SELECT t + 1000 FROM n1 WHERE id = 'n';       -- 1000
SELECT s + 'x' FROM n1 WHERE id = 'n';        -- 'x'
SELECT f * 2 FROM n1 WHERE id = 'n';          -- null
SELECT f / 2 FROM n1 WHERE id = 'n';          -- null
SELECT f % 2 FROM n1 WHERE id = 'n';          -- null
SELECT f + f FROM n1 WHERE id = 'n';          -- null   (both null)

SELECT id FROM n1 WHERE f + 0 = 0;            -- n      (a null row satisfies an arithmetic equality)
SELECT id FROM n1 WHERE f - 1 < 0;            -- n
SELECT id FROM n1 WHERE f < 1;                -- (none: the comparison rules treat null as no match)
```

`+` and `-` return the other operand when one side is null; `*`, `/` and `%` return null. So `f + c > p` is not `f > p - c`: the first admits every null row whenever `c > p` holds. The fuzzer spells its arithmetic twins with the null rows handled explicitly (`... AND f IS NOT NULL`, `... OR f IS NULL` for `<>`), so the shape is filed by the probe rather than by a campaign.
