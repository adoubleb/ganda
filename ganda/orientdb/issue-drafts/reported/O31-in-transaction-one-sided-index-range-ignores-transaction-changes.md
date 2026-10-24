# Inside a transaction, a one-sided range served by an index ignores the transaction's own writes

**OrientDB version:** 3.2.56 (official Docker image `orientdb:3.2.56`)
**Java version:** OpenJDK 1.8.0_502
**OS:** Ubuntu 26.04 (container)

## Steps to reproduce

```sql
CREATE CLASS t;
CREATE PROPERTY t.id STRING;
CREATE PROPERTY t.f LONG;
CREATE INDEX t_f ON t (f) NOTUNIQUE;
INSERT INTO t SET id = 'b', f = -7;
INSERT INTO t SET id = 'c', f = 5;
```

Run each block as one SQL script (in the console: `script sql ... end`):

```sql
BEGIN;
UPDATE t SET f = -100 WHERE id = 'c';
LET $r = (SELECT id FROM t WHERE f < 0);
ROLLBACK;
RETURN $r;        -- b          (expected b, c)

BEGIN;
UPDATE t SET f = -100 WHERE id = 'c';
LET $r = (SELECT id FROM (SELECT FROM t) WHERE f < 0);
ROLLBACK;
RETURN $r;        -- b, c       (a scan is right)

BEGIN;
UPDATE t SET f = -100 WHERE id = 'c';
LET $r = (SELECT id FROM t WHERE f > -200 AND f < 0);
ROLLBACK;
RETURN $r;        -- b, c       (a two-sided range through the index is right)
```

## Expected behavior

Every query returns `b, c`. Inside the transaction `c.f` is `-100`. Each `LET` runs before the `ROLLBACK`, which the scan's answer shows.

## Actual behavior

The one-sided range `f < 0` served by `t_f` returns only `b`, so it reads the committed index entries and ignores the transaction's update. The scan and the two-sided range in the same transaction are right.

## Scope

- **Affected:**
  - `<`, `<=`, `>`, `>=` served by an SB-tree index (`NOTUNIQUE` or `UNIQUE`, including the lead field of a composite index).
  - The result is inverted as well: `f > 0` in the same transaction still returns `c`, matching its old key.
  - A record inserted by the transaction is never found by such a range.
- **Correct inside the same transaction:**
  - Equality and `IN`.
  - Two-sided ranges and `BETWEEN`.
  - `ORDER BY f` served from the index.
  - Scans and deletes.
  - Once committed, `f < 0` returns `b, c`.

## Related: a range with reversed bounds throws inside a transaction

A range with reversed bounds matches nothing outside a transaction. Inside one, once the transaction has written the indexed field, the same query throws a raw exception:

With the same setup:

```sql
SELECT id FROM t WHERE f BETWEEN 6 AND 4;       -- no rows

BEGIN;
UPDATE t SET f = 9 WHERE id = 'c';
LET $r = (SELECT id FROM t WHERE f BETWEEN 6 AND 4);
ROLLBACK;
RETURN $r;
-- 500: java.lang.IllegalArgumentException: fromKey > toKey
```

## Report Status
Reported as 10886.