# B11 — `START` is a reserved word (cannot be a bare column alias)

**Bucket:** B (quirk, trivial). **Repro:** `python3 -I repro.py`

## Claim
`SELECT 1 AS start` is a syntax error (`257`), because `START` is reserved (START WITH / START
TRANSACTION). `AS strt` or a quoted `AS "start"` works.

## Actual (from repro.py)
```
SELECT 1 AS start FROM DUMMY  -> [257] incorrect syntax near "start"
SELECT 1 AS strt  FROM DUMMY  -> OK
SELECT 1 AS "start" (quoted)  -> OK
```

## How to judge
`START` is in HANA's reserved-words list — standard, documented, bucket B. Listed only because the
provider originally emitted `AS start` in traversal queries and it broke every rel+doc traversal.

## What the provider does
Aliases the traversal start column `strt`, not `start` (`concrete_syntax/traversal.py`).
