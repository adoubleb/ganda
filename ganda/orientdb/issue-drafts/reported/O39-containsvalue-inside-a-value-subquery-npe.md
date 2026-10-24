# O39 -- `CONTAINSVALUE` in the WHERE of a subquery whose answer is used as a value (`IN`, `NOT IN`, `LET`) is a NullPointerException

**Version:** OrientDB 3.2.56. Found by the fuzzer's write IR: `UPDATE n5 MERGE {...} WHERE id IN (SELECT id FROM n5 WHERE (f2 = :p0 OR f1 CONTAINSVALUE :p1))` (the `nest` target) answered a raw `java.lang.NullPointerException` on the one database that drew that spelling.

```sql
CREATE CLASS t; CREATE PROPERTY t.id STRING; CREATE PROPERTY t.g LONG; CREATE PROPERTY t.m EMBEDDEDMAP LONG;
CREATE CLASS j; CREATE PROPERTY j.src_id STRING;
INSERT INTO t SET id = 'r0', g = 0, m = {"k1": 1099511627776, "k2": 3};
INSERT INTO t SET id = 'r1', g = 1, m = {"k1": 5};
INSERT INTO j SET src_id = 'r0';
INSERT INTO j SET src_id = 'r1';

SELECT id FROM t WHERE m CONTAINSVALUE 1099511627776;                                  -- r0
SELECT id FROM t WHERE id IN (SELECT id FROM t WHERE m CONTAINSVALUE 1099511627776);   -- java.lang.NullPointerException
```

(The pivot is 2^40, outside int range, on purpose: with a small literal such as `7` even the top-level `m CONTAINSVALUE 7` answers nothing -- O7's map face, an Integer-boxed literal never equals a stored Long on a scan -- which would muddy the repro.)

The same `CONTAINSVALUE` filter throws wherever the subquery holding it is consumed as a VALUE -- an `IN` / `NOT IN` list, a `LET` variable, a write's filter -- whatever it projects and however the leaf is combined:

| shape | statement | answer |
|---|---|---|
| `IN` | `SELECT id FROM t WHERE id IN (SELECT id FROM t WHERE m CONTAINSVALUE 1099511627776)` | NPE (expected `r0`) |
| `NOT IN` | `SELECT id FROM t WHERE id NOT IN (SELECT id FROM t WHERE m CONTAINSVALUE 1099511627776)` | NPE (expected `r1`) |
| another projected column (not `id`, not an indexed field) | `SELECT id FROM t WHERE g IN (SELECT g FROM t WHERE m CONTAINSVALUE 1099511627776)` | NPE (expected `r0`) |
| a `LET` variable, only its size read -- no `IN` at all | `SELECT id, $a.size() AS n FROM t LET $a = (SELECT id FROM t WHERE m CONTAINSVALUE 1099511627776)` | NPE (expected `r0 1`, `r1 1`) |
| under a `NOT` | `SELECT id FROM t WHERE id IN (SELECT id FROM t WHERE NOT (m CONTAINSVALUE 1099511627776))` | NPE (expected `r1`) |
| one branch of an `OR` (the fuzzer's shape) | `SELECT id FROM t WHERE id IN (SELECT id FROM t WHERE m CONTAINSVALUE 1099511627776 OR g = 0)` | NPE (expected `r0`) |
| a write's filter | `DELETE FROM j WHERE src_id IN (SELECT id FROM t WHERE m CONTAINSVALUE 1099511627776)` | NPE (expected 1 row deleted) |

**Controls.** These isolate the cause to `CONTAINSVALUE` in a value-consumed subquery: the same subquery as a FROM target is fine, the other map operators in the same position are fine, and the same question spelled without `CONTAINSVALUE` is fine (and is the workaround):

| control | statement | answer |
|---|---|---|
| the subquery as the FROM target, not a value | `SELECT id FROM (SELECT FROM t WHERE m CONTAINSVALUE 1099511627776)` | `r0` (right) |
| `CONTAINSKEY` in the same position | `SELECT id FROM t WHERE id IN (SELECT id FROM t WHERE m CONTAINSKEY 'k1')` | `r0, r1` (right) |
| the same question through `values()` | `SELECT id FROM t WHERE id IN (SELECT id FROM t WHERE m.values() CONTAINS 1099511627776)` | `r0` (right) |
| ... or by key | `SELECT id FROM t WHERE id IN (SELECT id FROM t WHERE m.k1 = 1099511627776)` | `r0` (right) |

- **Affected:** a UNIQUE, NOTUNIQUE or hash index on `id`, or none; a bind parameter or a literal, with an Integer or a Long pivot.
- **Not affected:** `CONTAINSVALUE` at the top level, or in a subquery used as the FROM target.

**Fuzzer handling:** `guards.o39_predicate` is withheld wherever a CONTAINSVALUE leaf reaches such a subquery: the traversal IR's LET spellings and `rel:flat` (`o39_shape`), `node_in_subquery`'s inner predicate, `tx_read`'s `LET $r = (SELECT ...)`, and the write IR's `nest` target and the rel side's delete cascade. The read families rarely drew it, because a map pivot drawn from stored values is usually an int32, and O7's Integer-box face withholds every side first. Fact: `scripts/probe_osql.py`, "the write IR".

## Report Status
Reported as 10899. Confirmed.