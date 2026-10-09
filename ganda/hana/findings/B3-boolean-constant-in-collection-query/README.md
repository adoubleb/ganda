# B3 — BOOLEAN constant refused in a query that reads a collection

**Bucket:** B (quirk). **Repro:** `python3 -I repro.py`

## Claim
In any query scope that reads a COLLECTION, a BOOLEAN constant (`CASE … THEN TRUE`) is refused:
`[7] feature not supported: invalid constant, please use TO_JSON_BOOLEAN()`. Moving the boolean cast
one CTE removed from the collection read (a `raw` CTE over the collection, a `v` CTE over `raw`)
makes it pure SQL and it is accepted.

## Actual (from repro.py)
```
CASE..THEN TRUE over collection -> [7] invalid constant, please use TO_JSON_BOOLEAN()
same, in a subquery over coll   -> [7] invalid constant, please use TO_JSON_BOOLEAN()
CASE one CTE removed (two CTEs) -> OK  [('a', True), ('b', False)]
```

## How to judge
This is the Document Store SQL dialect disallowing a raw BOOLEAN literal where it is still parsing a
collection query. Confirm against the JSON Document Store Guide (TO_JSON_BOOLEAN section). It is a
dialect restriction, not a wrong answer — bucket B.

## What the provider does
Each main collection view is two CTEs: `raw_v_X` (collection text) and `v_X` (the casts over
`raw_v_X`), so `CASE … THEN TRUE` for a boolean column runs one step removed from the collection
(`concrete_syntax/document.py`, `view_definition`).
