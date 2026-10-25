# O16 -- an index lookup on a LONG column converts a double key by truncation: `f = 5.5` matches 5, `f >= 5.5` includes 5, `f < 5.5` excludes it; a scan compares through double

**Version:** OrientDB 3.2.56. Found by the fuzzer's cross-type pivots (a double pivot on a LONG column) in `node_filter_ids`: seed 431 of campaign c60b answered `f2 = 247.5` with the record whose `f2` is 247 on every side, because every side indexed the column (a NOTUNIQUE_HASH_INDEX on two, a composite NOTUNIQUE on the third) -- only the Python reference disagreed. The scan/index split was then isolated live.

```sql
CREATE CLASS c1; CREATE PROPERTY c1.id STRING; CREATE PROPERTY c1.f1 LONG;
INSERT INTO c1 SET id = 'a', f1 = 0; INSERT INTO c1 SET id = 'c', f1 = 5; INSERT INTO c1 SET id = 'd', f1 = 6;
INSERT INTO c1 SET id = 'f', f1 = 9007199254740993;           -- 2^53 + 1

-- class scan: LONG against DOUBLE goes through double
SELECT id FROM c1 WHERE f1 = 5.5;                             -- []
SELECT id FROM c1 WHERE f1 >= 5.5;                            -- [d, f]
SELECT id FROM c1 WHERE f1 < 5.5;                             -- [a, c]
SELECT id FROM c1 WHERE f1 = 9007199254740992.0;              -- [f]      (2^53 + 1 rounds to 2^53 as a double)

CREATE INDEX c1_f1 ON c1 (f1) NOTUNIQUE;                      -- UNIQUE behaves the same
SELECT id FROM c1 WHERE f1 = 5.5;                             -- [c]      the key is converted to the property type: 5
SELECT id FROM c1 WHERE f1 = -0.5;                            -- [a]      (truncation toward zero)
SELECT id FROM c1 WHERE f1 IN [5.5];                          -- [c]
SELECT id FROM c1 WHERE f1 >= 5.5;                            -- [c, d, f]
SELECT id FROM c1 WHERE f1 < 5.5;                             -- [a]
SELECT id FROM c1 WHERE f1 = 9007199254740992.0;              -- []       (exact long 2^53 against 2^53 + 1)
```

A NOTUNIQUE_HASH_INDEX shows the equality faces only (`= 5.5` matches 5) and answers the range predicates by a scan, correctly. The same question therefore has three answers depending on the plan. The fuzzer withholds, per side, a `=` / `IN` on an INT column with a double pivot that truncation changes (a fractional part, or a magnitude at or beyond 2^53) when the side indexes the column, and a `<` / `<=` / `>` / `>=` with such a pivot when a range-capable index (NOTUNIQUE, UNIQUE, DICTIONARY) serves it (`guards.o16_predicate`).


**Further faces (probed 3.2.56, the narrow numeric types):** the conversion is to the column's type, whatever it is. On an INTEGER column an index converts a Long key by WRAPPING: with `f` in `{0, 5, 2147483647, -2147483648}`, `f = 2147483648` through the index finds `-2147483648`, `f = 1099511627776` (2^40) finds `0`, `f > 2147483648` finds `0, 5, 2147483647` and `f < 2147483648` nothing -- a scan answers `[]`, `[]`, `[]` and everything. A SHORT column wraps the same way (`g = 65541` finds 5). On a FLOAT column an index ROUNDS a double key: with a stored `0.1` (the float 0.1f), `f = 0.1` matches through the index and nothing on a scan (which compares through double), and `f > 0.1` through the index excludes the record a scan includes. The fuzzer's guard now withholds any pivot the conversion to the column's type changes (`predicates.lossy_pivot`).


**Composite face (probed 3.2.56):** a double key on the second field of a composite index, next to an equality on its lead, is truncated the same way (`b = true AND f = 5.5` through an index on `(b, f)` finds the record whose `f` is 5), on the SB-tree and the hash kinds alike.


**Index-door face (probed 3.2.56):** `SELECT rid FROM index:<name> WHERE key = 2.5` on a LONG index answers the records whose key is 2, on the SB-tree and the hash kinds alike -- the `index:` door converts the key like a lookup does. The fuzzer's `:index` variant of `node_filter_ids` is withheld for a lossy pivot (`guards.index_door_known`).
