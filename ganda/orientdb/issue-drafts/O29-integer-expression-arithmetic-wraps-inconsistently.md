# O29 -- INTEGER expression arithmetic: `+` promotes to a long on a positive overflow but wraps on a negative one, `-` and `*` wrap in 32 bits

**Version:** OrientDB 3.2.56. Found while probing arithmetic over the field for the fuzzer's `<side>:arith` predicate spelling.

```sql
CREATE CLASS n1; CREATE PROPERTY n1.id STRING; CREATE PROPERTY n1.f INTEGER;
INSERT INTO n1 SET id = 'max', f = 2147483647;
INSERT INTO n1 SET id = 'min', f = -2147483648;
INSERT INTO n1 SET id = 'five', f = 5;

SELECT f + 1 FROM n1 WHERE id = 'max';        -- 2147483648   (promoted to a long)
SELECT f + f FROM n1 WHERE id = 'max';        -- 4294967294   (promoted)
SELECT f + f FROM n1 WHERE id = 'min';        -- 0            (WRAPPED: -2^32 does not fit an int; expected -4294967296)
SELECT f + :c FROM n1 WHERE id = 'min';       -- :c = -1  ->  2147483647   (wrapped; expected -2147483649)
SELECT f - 1 FROM n1 WHERE id = 'min';        -- -2147483649  (promoted)
SELECT f - :c FROM n1 WHERE id = 'max';       -- :c = -1  ->  -2147483648  (wrapped; expected 2147483648)
SELECT f * 2 FROM n1 WHERE id = 'max';        -- -2           (wrapped; expected 4294967294)
SELECT f * :c FROM n1 WHERE id = 'max';       -- :c = 8589934592 (a Long) -> -8589934592 (wrapped at 64 bits)
SELECT abs(f) FROM n1 WHERE id = 'min';       -- -2147483648  (Java's Math.abs)

SELECT id FROM n1 WHERE f * 2 > 0;            -- five          (the top value is missing: its product wrapped negative)
SELECT id FROM n1 WHERE f > 0;                -- five, max
```

The `+` operator checks for an overflow of two positive Integers and widens the result to a long, but not for two negative ones (`-2^31 + -2^31` is 0); `-` widens only one way as well; `*` never widens for two Integers (Java `int` arithmetic), and widens to a long -- which then wraps at 2^63 -- when one operand is a Long. The same expression over a LONG column wraps at 2^63 in every operator, which is documented Java behaviour; the INTEGER case is inconsistent with itself.

The fuzzer emits `(f <op> :c) <cmp> :pc` only where the arithmetic stays inside the column's box on every stored value (`predicates.arithmetic`), so this shape is filed by the probe rather than by a campaign.
