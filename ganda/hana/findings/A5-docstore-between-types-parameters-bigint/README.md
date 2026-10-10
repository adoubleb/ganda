# A5 / H5 — Document Store field: `BETWEEN ? AND ?` types its parameters BIGINT

**Bucket:** A (candidate HANA bug — a legal query fails). **Version:** HANA Cloud 4.00.000.00.
**Repro:** `python3 -I repro.py` (was B9, "context-dependent, does not minimise": it does, with
BOUND parameters; the earlier repro used literals)

## Claim
Over a field of a collection read through SQL, `f BETWEEN ? AND ?` with string parameters fails:
`[-10427] Conversion of parameter/column (1) from data type UCS2 (LE) to BIGINT failed`. The field
holds strings only. `f >= ? AND f <= ?` -- what SQL defines BETWEEN as -- answers.

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
