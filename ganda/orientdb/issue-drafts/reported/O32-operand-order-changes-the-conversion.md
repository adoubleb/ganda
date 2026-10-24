# O32 -- the comparison converts the RIGHT operand to the LEFT one's type: `:p = t` with a string pivot compares a DATETIME's Java `toString()`, `:p <= s` with a number parses the string

**Version:** OrientDB 3.2.56. Found while probing reversed operands (`:p > f`, a spelling the planner never serves from an index) for the fuzzer's `<side>:rev` variant.

```sql
CREATE CLASS n1; CREATE PROPERTY n1.id STRING; CREATE PROPERTY n1.t DATETIME; CREATE PROPERTY n1.d DATE; CREATE PROPERTY n1.s STRING; CREATE PROPERTY n1.f LONG;
INSERT INTO n1 SET id = 'a', t = '2024-01-01 12:34:56', d = '2024-01-01', s = 'abc', f = 9007199254740993;
INSERT INTO n1 SET id = 'b', t = '2024-06-30 23:59:59', d = '2024-06-30', s = '5', f = 5;

SELECT id FROM n1 WHERE t = :p;               -- :p = '2024-01-01 12:34:56'      -> a
SELECT id FROM n1 WHERE :p = t;               -- :p = '2024-01-01 12:34:56'      -> (none)
SELECT id FROM n1 WHERE :p = t;               -- :p = 'Mon Jan 01 12:34:56 UTC 2024' -> a     (the Date's toString())
SELECT id FROM n1 WHERE :p < t;               -- :p = '2024-01-01 12:34:56'      -> a, b     (a string comparison: '2...' < 'Mon...')
SELECT id FROM n1 WHERE :p = d;               -- :p = '2024-01-01' / '2024-01-01 00:00:00' -> (none)
SELECT id FROM n1 WHERE :p = t;               -- :p = 1704112496000 (epoch ms)   -> a        (a number on the left compares numerically)

SELECT id FROM n1 WHERE s >= :p;              -- :p = 4  -> a, b     (the number becomes the string '4')
SELECT id FROM n1 WHERE :p <= s;              -- :p = 4  -> b        (the strings are parsed as numbers; 'abc' does not parse and fails)
SELECT id FROM n1 WHERE f > :p;               -- :p = '10' -> a      (the string becomes a long)
SELECT id FROM n1 WHERE :p < f;               -- :p = '10' -> a, b   ('10' < '5' as strings)

SELECT id FROM n1 WHERE :p = f;               -- :p = 9007199254740992.0 -> a   (number against number is symmetric: the wider box)
```

The comparison operators convert the right-hand value to the left-hand value's type, so a query is not symmetric under operand exchange whenever the pivot's type differs from the column's: a string pivot on a date column, a numeric pivot on a string column, a string pivot on a numeric column. Number-against-number, string-against-string and a `COLLATE ci` column compare alike in either order (probed). The fuzzer withholds the reversed spelling of a string pivot on a DATETIME / DATE column (`guards.o32_predicate`) and never draws numeric pivots on string columns.

## Report Status
Reported as 10888. Confirmed.