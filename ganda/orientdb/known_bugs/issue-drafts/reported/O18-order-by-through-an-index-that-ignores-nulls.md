# O18 -- `ORDER BY f DESC` served from an index created with `ignoreNullValues: true` returns no rows; `ORDER BY f ASC` omits the records whose `f` is null

**Version:** OrientDB 3.2.56. Found by the fuzzer's `node_max_vs_order` family (six seeds of campaign c60c, INT and DOUBLE fields, on the class and on the abstract base class) the moment the index DDL started spelling `ignoreNullValues: true` out: `SELECT f FROM t WHERE f IS NOT NULL ORDER BY f DESC LIMIT 1` answered nothing on every side holding such an index while `max(f)` answered the value.

```sql
CREATE CLASS t; CREATE PROPERTY t.f LONG; CREATE PROPERTY t.g STRING;
INSERT INTO t SET g = 'a', f = 5; INSERT INTO t SET g = 'b', f = 1; INSERT INTO t SET g = 'c', f = null;
INSERT INTO t SET g = 'd', f = 9; INSERT INTO t SET g = 'e';
CREATE INDEX t_f ON t (f) NOTUNIQUE METADATA {ignoreNullValues: true};     -- UNIQUE behaves the same

SELECT g FROM t ORDER BY f DESC;                          -- []              expected [c, e, d, a, b]
SELECT g FROM t WHERE f IS NOT NULL ORDER BY f DESC;      -- []              expected [d, a, b]
SELECT g FROM t ORDER BY f ASC;                           -- [b, a, d]       expected [b, a, d, c, e]
SELECT g FROM t ORDER BY f DESC, g ASC;                   -- [c, e, d, a, b] (a second key: a scan and a sort, correct)
SELECT g FROM t WHERE f > 0 ORDER BY f DESC;              -- [d, a, b]       (a range through the index, then a sort: correct)
SELECT max(f) FROM t;                                     -- 9
```

`EXPLAIN` shows `FetchFromIndexValuesStep` for the single-key order: the planner walks the index instead of sorting, and with nulls excluded from the index the descending walk yields nothing and the ascending walk lacks the null records (which a sort puts last). The same index without the metadata (the default, nulls indexed) and a NOTUNIQUE_HASH_INDEX (never used for an order) are correct. The fuzzer withholds the `:order` side of `node_max_vs_order` for `max` where the side holds a range-capable index with `ignoreNullValues: true` on the field (`node_max_vs_order_cases`); `node_ordered_page` always carries the `id` tiebreak, which routes it through a sort.


**Further face (probed 3.2.56):** a COMPOSITE index created with `ignoreNullValues: true` is walked for `ORDER BY f ASC, g ASC` (and for the prefix `ORDER BY f ASC`) and lacks every record with a null in ANY key column -- `[r2, r0, r6]` out of seven -- while `ORDER BY f DESC, g DESC` answers nothing. The same index with `ignoreNullValues: false` keeps the records but inverts the null placement (O23). The fuzzer's `node_ordered_keys` family, whose keys carry no id tiebreak, is withheld per side at both shapes (`guards.ordered_keys_known`).

**ASC face (campaign c100e, seed 1049).** With a COMPOSITE index that ignores nulls, a record whose OTHER component is null has no entry at all, so the ascending walk skips it -- `ORDER BY f ASC LIMIT 1` is no longer `min(f)`:

```sql
CREATE CLASS cw; CREATE PROPERTY cw.f LONG; CREATE PROPERTY cw.g LONG;
INSERT INTO cw SET f = 1, g = 10; INSERT INTO cw SET f = -5; INSERT INTO cw SET f = 7, g = 70;
CREATE INDEX cw_fg ON cw (f, g) NOTUNIQUE METADATA {ignoreNullValues: true};
SELECT min(f) FROM cw;                                        -- -5
SELECT f FROM cw WHERE f IS NOT NULL ORDER BY f ASC LIMIT 1;  -- 1        (the index walk; -5 has no entry)
SELECT f FROM cw ORDER BY f ASC, id ASC;                      -- -5, 1, 7 (the tiebreak forces a sort)
```

## Report Status
Reported as 10885. Confirmed and fixed.