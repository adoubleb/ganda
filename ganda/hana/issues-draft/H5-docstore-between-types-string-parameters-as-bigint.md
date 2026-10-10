# H5 -- SQL over a Document Store field: `BETWEEN ? AND ?` types its parameters BIGINT, `>= ? AND <= ?` does not

**Version:** SAP HANA Cloud, `SYS.M_DATABASE.VERSION` = `4.00.000.00.1788859455` (2026-10), JSON
Document Store (collections) queried through SQL.
**Client:** `hdbcli`, plain SQL (`cursor.execute` with bound parameters).
**Found by:** the Ganda SAP HANA fuzzer's round-3 campaign, as an error asymmetry on a 6-view doc
traversal join with a string `BETWEEN` (filed then as B9, "context-dependent, does not minimise"):
the isolated repro used literals, and the trigger is a BOUND parameter. Minimised 2026-10-10.

## Summary

Over a field of a collection read through SQL (`WITH raw AS (SELECT $."f" AS f FROM c)`), a filter
`f BETWEEN ? AND ?` with string parameters fails: `[-10427] Conversion of parameter/column (1) from
data type UCS2 (LE) to BIGINT failed (invalid number: not a valid number string 'A')`. HANA has typed
the PARAMETERS as `BIGINT`, though the field holds strings only. The comparisons `BETWEEN` is defined
as -- `f >= ? AND f <= ?` -- answer, as do `f = ?`, `f > ?`, `f IN (?, ?)`, the same `BETWEEN` with
literals, and the same `BETWEEN` over an `NVARCHAR` column table.

## Steps to reproduce

```sql
CREATE COLLECTION c;
INSERT INTO c VALUES('{"f": "abc"}');
INSERT INTO c VALUES('{"f": "xyz"}');

-- parameters bound as the strings 'A' and 'i'
WITH raw AS (SELECT $."f" AS f FROM c)
SELECT f FROM raw WHERE f BETWEEN ? AND ?;
--   [-10427] Conversion of parameter/column (1) from data type UCS2 (LE) to BIGINT failed
--            (invalid number: not a valid number string 'A')

WITH raw AS (SELECT $."f" AS f FROM c)
SELECT f FROM raw WHERE f >= ? AND f <= ?;           -- ['abc']
```

## Expected vs actual

- **Expected:** both queries answer `['abc']`. SQL defines `X BETWEEN Y AND Z` as `X >= Y AND X <= Z`;
  the second query is the first one spelled out, and a string field compared with string parameters
  has no numeric reading.
- **Actual:** the `BETWEEN` form fails converting its first parameter to `BIGINT`.

## Characterisation (each bullet an isolated trial, `H5_repro.py` and the Ganda probes)

- Only `BETWEEN` with bound parameters fails. `>= ? AND <= ?`, `> ?`, `= ?`, `IN (?, ?)` and
  `BETWEEN 'A' AND 'i'` (literals) answer correctly over the same field.
- It does not depend on the data: the field holding only `"abc"` / `"xyz"`, only numeric-looking
  strings (`"0"`, `"12"`), or a mix, fails the same way.
- Typing the field (`TO_NVARCHAR(f) BETWEEN ? AND ?`) or the parameters (`CAST(? AS NVARCHAR(10))`)
  makes it answer.
- The same `BETWEEN ? AND ?` over a column table with an `NVARCHAR` column answers.
- It fails inside larger queries alike (a view joined to another collection view; a further
  numeric comparison on another column): the shape the fuzzer first met, in a 6-view traversal
  join.

## Impact

A parameterised range filter over a JSON string field -- `WHERE name BETWEEN ? AND ?`, the usual
prepared-statement form of a range search -- fails with a conversion error, while the same filter
written with `>=` / `<=` works. An application that binds its values (as it should, against
injection) cannot use `BETWEEN` on a collection's string fields without casting.

## Workaround

Type the field (`TO_NVARCHAR(f)`) or the parameters (`CAST(? AS NVARCHAR(n))`), or spell the range
as `f >= ? AND f <= ?`. The Ganda HANA provider types every string field of its doc views with
`TO_NVARCHAR` (`concrete_syntax/document.py`, `sql_cast`), which also keeps a numeric-looking value
a string.

## Repro script

`H5_repro.py` in this folder is a self-contained reproduction (needs only `hdbcli`, connects from
`HANA_*`, works in a scratch schema it drops); `H5_repro_output.txt` is its transcript against HANA
Cloud 4.00.000.00.
