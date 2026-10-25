# O6 -- `ORDER BY` sorts strings by locale collation while `<`/`>`/`max()` use UTF-16 code-unit order

**Version:** OrientDB 3.2.56. Found while building the reference leg for the ordered-page family.

```sql
CREATE CLASS o1; CREATE PROPERTY o1.f3 STRING;
INSERT INTO o1 SET f3 = 'a'; INSERT INTO o1 SET f3 = 'B'; INSERT INTO o1 SET f3 = 'b'; INSERT INTO o1 SET f3 = 'z';
SELECT f3 FROM o1 ORDER BY f3 ASC;   -- a, b, B, z     (collation: case-insensitive primary, lowercase first)
SELECT f3 FROM o1 WHERE f3 > 'B';    -- a, b, z        (code units: 'B' = 66 < 'a' = 97)
SELECT max(f3) FROM o1;              -- z; with a U+FFFF row: U+FFFF; with an emoji row the emoji sorts BELOW U+FFFF in ORDER BY and above 'z' in >
SELECT f3 FROM o1 ORDER BY f3 DESC LIMIT 1;  -- disagrees with max(f3) whenever case or non-ASCII is involved
```

Full observed ASC order over a 28-value pool: `" ", "-", "_", "1", "a", "A", "ab", "aB", "Ab", "ab ", "a b", "a-b", "b", "B", "e", "é", "f", "i", "I", "İ", "ss", "ß", "z", "Z", "ı", "😀", "￿"` -- a `java.text.Collator` order (punctuation ignored at the primary level, lowercase before uppercase at the tertiary level), with the same order whether or not the field carries an index. `WHERE f3 > 'b'`, `BETWEEN`, `max`/`min` all use `String.compareTo`.

Consequences: `ORDER BY f LIMIT 1` and `max(f)` disagree; keyset pagination (`WHERE f > :last ORDER BY f`) skips and repeats rows. The fuzzer's `node_max_vs_order` family runs the two spellings as a same-database pair and withholds the STRING case as this known finding (`guards`), and the ordered-page reference leg is withheld for strings.

## Report Status
Reported as 10881. Confirmed as bug.