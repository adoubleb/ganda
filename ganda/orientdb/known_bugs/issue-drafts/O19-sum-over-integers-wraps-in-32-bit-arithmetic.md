# O19 -- `sum()` over INTEGER values accumulates in 32-bit arithmetic and wraps silently; `+` on the same values promotes to a long, `*` wraps

**Version:** OrientDB 3.2.56. Found while probing the INTEGER column type before the fuzzer's `narrow_type_probability` axis went live, then by campaign smoke3 (seed 515) on the doc side's EMBEDDED edge attributes: `SELECT id, sum(e3.f1) AS v FROM n2 GROUP BY id` answered `-2147483070` where the graph side's `sum(outE('e3').f1)` over the same values declared LONG answered `2147484226` (the correct long).

```sql
CREATE CLASS i1; CREATE PROPERTY i1.id STRING; CREATE PROPERTY i1.f INTEGER;
INSERT INTO i1 SET id = 'b', f = 5; INSERT INTO i1 SET id = 'c', f = 2147483647;

SELECT sum(f) FROM i1;                        -- -2147483644     expected 2147483652
SELECT f + 1 FROM i1 WHERE id = 'c';          -- 2147483648      (promoted to a long: correct)
SELECT f * 2 FROM i1 WHERE id = 'c';          -- -2              (wrapped)
SELECT math_abs(f) FROM i1 WHERE id = 'd';    -- -2147483648     for f = -2147483648 (Java's Math.abs)

-- the same through an embedded list (the values are Integers because the JSON fits an int)
CREATE CLASS em; CREATE PROPERTY em.e EMBEDDEDLIST;
INSERT INTO em SET e = [{"k": 2147483647}, {"k": 5}];
SELECT sum(e.k) FROM em;                      -- -2147483644
```

`sum()` keeps the box of its first operand and adds in it (`Integer + Integer` in int arithmetic), so a column whose values individually fit an int but whose total does not answers a wrapped number with no error, whereas the `+` operator on the same values promotes on overflow. A LONG column (Long boxes) sums correctly up to 2^63. The fuzzer never emits a sum over an INTEGER column beyond int32 (`reference.sum_is_exact`), and withholds the doc side's `esum` over an INT edge attribute whose stored values all fit an int32 while their total does not (`guards.o19_shape`).
