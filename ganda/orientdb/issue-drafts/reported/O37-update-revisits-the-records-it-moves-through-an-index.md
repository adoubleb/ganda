# `UPDATE t SET f = f + 1 WHERE f > 4` never terminates when `f` is indexed

**OrientDB version:** 3.2.57 (official Docker image `orientdb:3.2.57`; also seen on 3.2.56)
**Java version:** OpenJDK 1.8.0_504
**OS:** Ubuntu 26.04 (container)
**Found by:** probing the fuzzer's write IR (predicate-targeted writes); the first probe hung the server at 120% CPU with one record's `f` climbing past eleven million.

## Steps to reproduce

On an empty database:

```sql
CREATE CLASS t;
CREATE PROPERTY t.f LONG;
CREATE INDEX t_f ON t (f) NOTUNIQUE;
INSERT INTO t SET f = 5;   -- one record

UPDATE t SET f = f + 1 WHERE f > 4 AND f < 100;   -- count: 95   (Wrong: expected count: 1)
SELECT f FROM t;                                  -- 100         (Wrong: expected 6)

-- without the upper bound the UPDATE never returns; the TIMEOUT clause only stops it
UPDATE t SET f = f + 1 WHERE f > 4 TIMEOUT 3000 EXCEPTION;   -- Timeout expired   (expected count: 1)
SELECT f FROM t;                                             -- e.g. 92300, varies per run: the partial writes stay committed   (expected 101)
```

## Expected behavior

Each `UPDATE` matches the one record, increments it once and returns `count: 1`. The first leaves `f = 6`, the second adds 1 more.

## Actual behavior

- The first `UPDATE` returns `count: 95` and leaves `f = 100`: the record was incremented 95 times.
- The second `UPDATE` fails with `Timeout expired` after 3 seconds, and `f` is left at a large value that changes from run to run (92300, 185900 and 297900 in three runs): the writes made before the timeout stay committed. Without the `TIMEOUT` clause the statement never returns.

Without the index (leave out `CREATE INDEX`), both statements update the record once and return `count: 1`.

## Why this is a bug

An `UPDATE` must modify each matching record once. Here the same record is written repeatedly, so the statement returns the wrong count and leaves the wrong value, or never terminates. Whether that happens depends only on whether an index exists on `f`.

## Report status

Reported as 10913