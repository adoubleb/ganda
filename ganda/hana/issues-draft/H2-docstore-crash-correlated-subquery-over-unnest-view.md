# H2 -- Document Store crashes (internal error) on a correlated scalar subquery over an UNNEST view

**Version:** SAP HANA Cloud, `SYS.M_DATABASE.VERSION` = `4.00.000.00.1788859455` (2026-10), JSON
Document Store (collections) queried through SQL.
**Client:** `hdbcli`, plain SQL (`cursor.execute`).
**Found by:** the Ganda SAP HANA fuzzer's first live traversal campaign (`ganda hana run --seed 1`),
as an `internal_error` on two `doc` traversal shapes (`proj=distinct_count` and `proj=reach2_count`):
the correlated-scalar-subquery rendering of a per-start aggregate crashed, while the equivalent
LEFT JOIN + GROUP BY rendering answered.

## Summary

A **correlated scalar subquery** whose inner query reads a COLLECTION through `UNNEST` fails with an
internal error -- `[3] fatal error: Execution flow must not reach here` -- instead of answering (or
raising a clean "feature not supported"). The crash needs all three of: the subquery is a scalar
subquery in the SELECT list, it is correlated to the outer row, and its FROM is (or derives from) a
view/subquery that reads a collection via `UNNEST`. Drop the correlation, or the `UNNEST`, and it
answers.

## Steps to reproduce

```sql
CREATE COLLECTION pc;
INSERT INTO pc VALUES('{"id": "a", "links": [{"to": "b"}]}');

-- a view over the collection (plain) and one that UNNESTs an embedded array
WITH node AS (SELECT $."id" AS id FROM pc),
     link AS (SELECT $."id" AS src FROM pc UNNEST $."links" AS "l")

-- (1) UNCORRELATED scalar subquery over the UNNEST view: OK
SELECT s.id, (SELECT COUNT(*) FROM link) AS v FROM node s;                       -- [a, 1]

-- (2) CORRELATED scalar subquery over the UNNEST view: CRASH
SELECT s.id, (SELECT COUNT(*) FROM link p WHERE p.src = s.id) AS v FROM node s;
--   [3] fatal error: Execution flow must not reach here. See error trace for details

-- (3) CORRELATED scalar subquery over a NON-UNNEST collection view: OK
WITH node AS (SELECT $."id" AS id FROM pc), other AS (SELECT $."id" AS k FROM pc)
SELECT s.id, (SELECT COUNT(*) FROM other p WHERE p.k = s.id) AS v FROM node s;    -- [a, 1]
```

## Expected vs actual

- **Expected:** query (2) answers one row per `node` with the count of matching `link` rows (the
  usual correlated "children per parent" pattern), exactly as the LEFT JOIN + GROUP BY form does:
  `SELECT s.id, COUNT(p.src) FROM node s LEFT JOIN link p ON p.src = s.id GROUP BY s.id`.
- **Actual:** query (2) aborts the statement with `[3] fatal error: Execution flow must not reach
  here` -- an internal assertion, not a feature-support rejection (which is code 7) and not a plan
  error.

## Characterisation (each bullet an isolated trial)

- The crash needs the subquery to be BOTH scalar-in-SELECT and correlated: the uncorrelated form (1)
  answers.
- The crash needs the inner relation to read a collection through `UNNEST`: the same correlated
  shape over a non-UNNEST collection view (3) answers, and over plain COLUMN TABLES it answers.
- `COUNT(*)`, `COUNT(DISTINCT ...)` and other aggregates in the scalar subquery all crash the same
  way; the join-plus-group-by rewrite of any of them answers.
- A single collection view is enough; no UNION, no self-join, and no second collection are needed
  (the fuzzer first hit it on a 3-branch reach and a 2-table join, but it reduces to the above).

## Impact

The natural correlated form of a per-row aggregate over an embedded array -- "for each document, the
count/sum/max over its (or a related document's) array elements" -- aborts the whole statement with
an internal error. An application must know to rewrite every such query as an explicit outer join
grouped by the correlation key.

## Workaround

Rewrite the correlated scalar subquery as a LEFT JOIN of the UNNEST relation to the outer relation,
grouped by the correlation key. The Ganda HANA provider does this: it withholds the `scalar`
(correlated-subquery) rendering of a doc-model aggregate / reach count and keeps only the `join`
(LEFT JOIN + GROUP BY) rendering (`concrete_syntax/traversal.py`, `applicable`).

## Repro script

`H2_repro.py` in this folder is a self-contained reproduction (needs only `hdbcli`, connects from
`HANA_*`); `H2_repro_output.txt` is its transcript against HANA Cloud 4.00.000.00.
