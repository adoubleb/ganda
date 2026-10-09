# B4 — collection-filter LIKE right-hand side must be a constant literal

**Bucket:** B (quirk). **Repro:** `python3 -I repro.py`

## Claim
In a collection query filter, the RHS of `LIKE` must be a constant string literal (or
`TO_NVARCHAR`/`TO_CHAR`/`UPPER`/`LOWER` of one). A bound parameter — even cast — is refused:
`[7] feature not supported: Right-hand side of LIKE must be a constant string literal or TO_NVARCHAR,
TO_CHAR, UPPER and LOWER functions`.

## Actual (from repro.py)
```
LIKE CAST(? AS NVARCHAR)    -> [7] Right-hand side of LIKE must be a constant string literal ...
LIKE TO_NVARCHAR(?)         -> [7] (parameter type unknown) / rejected
LIKE 'y%' (constant literal)-> OK  [('b',)]
```

## How to judge
A grammar restriction of the collection filter language — check the JSON Document Store Guide's
filter grammar. Not a wrong answer; bucket B.

## What the provider does
Inlines the LIKE pattern as an escaped SQL string literal rather than binding it
(`concrete_syntax/document.py`, `doc_pred` + `_sql_string`).
