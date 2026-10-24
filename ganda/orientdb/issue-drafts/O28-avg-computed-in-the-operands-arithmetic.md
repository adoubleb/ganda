# O28 -- `avg()` divides in the operands' arithmetic: integer division over integer columns (`avg(1, 2, 2)` is 1), a wrapped sum over INTEGERs, the sum's scale over DECIMALs

**Version:** OrientDB 3.2.56. Found while probing the aggregate functions for the fuzzer's `node_agg` family.

```sql
CREATE CLASS n1; CREATE PROPERTY n1.f1 LONG; CREATE PROPERTY n1.f2 INTEGER; CREATE PROPERTY n1.f3 DECIMAL; CREATE PROPERTY n1.f4 DOUBLE;
INSERT INTO n1 SET f1 = 1, f2 = 1, f3 = 0.1, f4 = 0.1;
INSERT INTO n1 SET f1 = 2, f2 = 2, f3 = 0.2, f4 = 0.2;
INSERT INTO n1 SET f1 = 2, f2 = 2147483647, f3 = 0.4, f4 = 0.4;

SELECT avg(f1) FROM n1;                                   -- 1            expected 1.6666...
SELECT avg(f1) FROM n1 WHERE f1 IN [1, 2] AND f2 < 3;     -- (1 + 2) / 2 = 1
SELECT avg(f2) FROM n1 WHERE f2 > 1;                      -- -1073741823  (2 + 2147483647 wraps in 32-bit arithmetic, then divides)
SELECT avg(f3) FROM n1;                                   -- 0.2          expected 0.2333... (0.7 / 3 rounded to the sum's scale, 1)
SELECT avg(f3) FROM n1 WHERE f3 < 0.4;                    -- 0.2          expected 0.15 (0.3 / 2, HALF_UP at scale 1)
SELECT avg(f4) FROM n1;                                   -- 0.2333333333333333   (doubles divide correctly)
SELECT median(f1) FROM n1;                                -- 2.0          (median promotes)
```

The function keeps the box of its operands through the sum and the division: a long sum divided by a long count truncates, an int sum wraps (as `sum()` does, O19), a BigDecimal sum is divided at its own scale. The fuzzer emits `avg` over integer columns only, expects the exact mean, and withholds every side where that mean is not integral (`guards.o28_avg`).
