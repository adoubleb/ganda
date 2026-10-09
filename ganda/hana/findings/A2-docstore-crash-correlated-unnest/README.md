# A2 / H2 — Document Store crashes on a correlated scalar subquery over an UNNEST view

**Bucket:** A (candidate HANA bug — internal crash). **Version:** HANA Cloud 4.00.000.00.
**Repro:** `python3 -I repro.py`

## Claim
A correlated scalar subquery whose inner query reads a collection through `UNNEST` aborts with
`[3] fatal error: Execution flow must not reach here` — an internal assertion, not a clean
"feature not supported" (code 7) and not a plan error.

## Minimal trigger (all three needed)
1. the subquery is scalar-in-SELECT, **and**
2. it is correlated to the outer row, **and**
3. its FROM derives from a view that reads a collection via `UNNEST`.

Drop any one and it answers (the repro shows all three isolating cases).

## Expected
The query answers one row per outer row (the usual correlated "children per parent" count), exactly
as the equivalent `LEFT JOIN … GROUP BY` does.

## Actual (from repro.py)
```
uncorrelated over UNNEST view : OK  [('a', 1)]
CORRELATED  over UNNEST view  : [3] fatal error: Execution flow must not reach here
correlated, no-UNNEST view    : OK  [('a', 1)]
```

## How to judge
A code-3 internal assertion for a well-formed query is an engine defect by any reading. Confirm the
LEFT JOIN rewrite answers the same question (so it is not a semantic limitation). The fuzzer first hit
it on a 3-branch reach and a 2-table join; it reduces to the single-collection case in the repro.

## What the provider does
Withholds the `scalar` (correlated-subquery) rendering of a doc-model aggregate / reach count and
keeps only the `join` (LEFT JOIN + GROUP BY) rendering
(`concrete_syntax/traversal.py`, `applicable`).
