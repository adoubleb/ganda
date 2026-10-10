# B4 — collection-filter LIKE: `CAST(? AS NVARCHAR)` is accepted, the same cast with a length is not

**Bucket:** B — not a bug; a minor undocumented restriction (verified 2026-10-10). **Repro:** `python3 -I repro.py`

## Claim (corrected 2026-10-10)
In a collection query's filter, LIKE's right-hand side may be a constant literal, `TO_NVARCHAR` /
`UPPER` / `LOWER` of one, or the documented parameter form `CAST(? AS NVARCHAR)`. The same cast
WITH a length, `CAST(? AS NVARCHAR(5000))`, is refused (`[7] Right-hand side of LIKE must be a
constant string literal ...`), though `= CAST(? AS NVARCHAR(5000))` is accepted. The first write-up
tested only the length form and concluded "a parameter is refused".

## Actual (from repro.py)
```
LIKE CAST(? AS NVARCHAR)       -> OK [('b',)]
LIKE CAST(? AS NVARCHAR(5000)) -> [7] Right-hand side of LIKE must be a constant string literal ...
LIKE 'y%' (constant literal)   -> OK [('b',)]
```

## Verdict
**Not a bug.** The JSON Document Store Guide allows `CAST(? AS NVARCHAR)` in WHERE for SELECT /
UPDATE / DELETE (https://help.sap.com/docs/hana-cloud-database/sap-hana-cloud-sap-hana-database-json-document-store-guide/sql-parameters-and-prepared-statements) and says LIKE works as for tables
(https://help.sap.com/docs/hana-cloud-database/sap-hana-cloud-sap-hana-database-json-document-store-guide/select-statement); the documented form works. That a length makes the parser refuse it is
undocumented, and worth a doc note at most.

## What the provider does
Inlines the LIKE pattern as an escaped string literal (`concrete_syntax/document.py`, `doc_pred`).
`CAST(? AS NVARCHAR)` would bind it instead.
