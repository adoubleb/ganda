# A5 / H5 — Document Store field: `BETWEEN ? AND ?` types its parameters BIGINT

**Bucket:** A (candidate HANA bug — a legal query fails). **Version:** HANA Cloud 4.00.000.00.
**Repro:** `python3 -I repro.py` (was B9, "context-dependent, does not minimise": it does, with
BOUND parameters; the earlier repro used literals)

## Claim
Over a field of a collection read through SQL, `f BETWEEN ? AND ?` with string parameters fails:
`[-10427] Conversion of parameter/column (1) from data type UCS2 (LE) to BIGINT failed`. The field
holds strings only. `f >= ? AND f <= ?` -- what SQL defines BETWEEN as -- answers.

## Steps to reproduce
Plain SQL. Steps 3 and 4 need BOUND parameters (`?`), e.g. `cursor.execute(sql, ['A', 'i'])` in
hdbcli; with the values written as constants the query answers.

1. Create a scratch schema and a collection whose field `f` holds strings only:
   ```sql
   CREATE SCHEMA A5_REPRO;
   SET SCHEMA A5_REPRO;
   CREATE COLLECTION c;
   INSERT INTO c VALUES('{"f": "abc"}');
   INSERT INTO c VALUES('{"f": "xyz"}');
   ```
2. A column table with the same field, for the control:
   ```sql
   CREATE COLUMN TABLE t (f NVARCHAR(5000));
   INSERT INTO t VALUES ('abc');
   ```
3. `BETWEEN` with bound strings, binding `('A', 'i')`:
   ```sql
   WITH raw AS (SELECT $."f" AS f FROM c) SELECT f FROM raw WHERE f BETWEEN ? AND ?;
   ```
   -> **`[-10427] Conversion of parameter/column (1) from data type UCS2 (LE) to BIGINT failed`** (bug).
4. What `BETWEEN` is defined as, the same binds -> `abc`:
   ```sql
   WITH raw AS (SELECT $."f" AS f FROM c) SELECT f FROM raw WHERE f >= ? AND f <= ?;
   ```
5. Controls, each -> `abc`: `... WHERE f BETWEEN 'A' AND 'i'` (constants);
   `... WHERE TO_NVARCHAR(f) BETWEEN ? AND ?`; `SELECT f FROM t WHERE f BETWEEN ? AND ?`.
6. Clean up: `DROP SCHEMA A5_REPRO CASCADE;`

Reproduced on 4.00.000.00.1788856605 (2026-10-23).

## Actual (from repro.py)
```
collection field: f BETWEEN ? AND ?     : [-10427] ... to BIGINT failed (... 'A')
collection field: f >= ? AND f <= ?     : [('abc',)]
collection field: BETWEEN 'A' AND 'i'   : [('abc',)]
collection field: TO_NVARCHAR(f) BETWEEN: [('abc',)]
column table:     f BETWEEN ? AND ?     : [('abc',)]
```

## How to judge
`X BETWEEN Y AND Z` is `X >= Y AND X <= Z`; one form answers and the other fails on the same field
and parameters, independent of the data (all-string, numeric-looking, or mixed values alike). Not
documented anywhere (searched the JSON Document Store Guide and the SQL Reference, 2026-10-10).

## What the provider does
Types every string field of its doc views `TO_NVARCHAR` (`concrete_syntax/document.py`, `sql_cast`).
Write-up: `../../issues-draft/H5-docstore-between-types-string-parameters-as-bigint.md`.
