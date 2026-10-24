# `IN (subquery)` throws `NoSuchElementException` on a hash-indexed property when the subquery returns an empty list

**OrientDB version:** 3.2.57 (official Docker image `orientdb:3.2.57`; also seen on 3.2.56)
**Java version:** OpenJDK 1.8.0_504
**OS:** Ubuntu 26.04 (container)
**Found by:** the LLM-card campaigns: first in `cards-round2-c100` (cards `group-round2-0206`, `reach-round2-0154`; 8 bundles), then the 12-hour corpus-only campaign `cards12h` (180 bundles over five cards: `group-round2-0206`, `reach-round2-0154`, `reach-round2-0276`, `reach-round2-0327`, `reach-round2-0015`). Pinned in `scripts/probe_osql.py` ("O46"); guarded for the cards in `fuzzer/cards/known.py` (`_o46_hash_in`).

## Steps to reproduce

On an empty database:

```sql
CREATE CLASS t;
CREATE PROPERTY t.id STRING;
CREATE INDEX t_id ON t (id) UNIQUE_HASH_INDEX;
INSERT INTO t SET id = 'a', l = ['b'];
INSERT INTO t SET id = 'b', l = [];   -- an empty list

SELECT id FROM t WHERE id IN (SELECT l FROM t);                 -- java.util.NoSuchElementException   (Wrong: expected b)

-- the same filter over a subquery target, where the index is not used
SELECT id FROM (SELECT FROM t) WHERE id IN (SELECT l FROM t);   -- b
```

## Expected behavior

Both queries return one row, `id = b`.

## Actual behavior

The first query fails with `java.util.NoSuchElementException` (the REST API answers `500` with that message; the console prints `Error on executing command: ... -> null`). The second query, the same filter over a subquery target, returns `b`.

The first query also fails with a `NOTUNIQUE_HASH_INDEX` (and, on 3.2.56, a `DICTIONARY` index). With a `UNIQUE` or `NOTUNIQUE` index, or no index, it returns `b`.

## Why this is a bug

The query is valid and has a well-defined answer, which the engine returns when the index is not used. Using a hash index on `id` turns it into a raw Java exception whenever one of the subquery's values is an empty list, such as a record with no outgoing links in an `id IN (SELECT <link path> ...)` query.

## Report status
Reported as 10910