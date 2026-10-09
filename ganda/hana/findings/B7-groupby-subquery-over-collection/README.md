# B7 — GROUP BY inside a subquery over a collection is refused

**Bucket:** B (quirk). **Repro:** `python3 -I repro.py`

## Claim
`SELECT COUNT(*) FROM (SELECT f GROUP BY f) z` directly over a collection is refused:
`[7] feature not supported: GROUP BY clause after GROUP BY clause on collection tables (perhaps due
to a subquery)`. The equivalent `COUNT(DISTINCT f)` at the top level works.

## Actual (from repro.py)
```
COUNT(*) over (GROUP BY sub) -> [7] GROUP BY clause after GROUP BY clause on collection tables ...
COUNT(DISTINCT f1) top-level -> OK
```

## How to judge
A restriction on nesting a GROUP BY in a collection subquery — check the Document Store Guide. Not a
wrong answer; bucket B.

## What the provider does
Withholds the `fn` distinct-count form (which nests a GROUP BY in a subquery) for the doc collection
query; the base `COUNT(DISTINCT)` form and the SQL views cover it
(`concrete_syntax/select.py`, `applicable`).
