# B6 — UNNEST view refused when nested inside a subquery

**Bucket:** B (quirk; and the engine fact behind A2). **Repro:** `python3 -I repro.py`

## Claim
The Document Store refuses an `UNNEST`-based view nested inside a subquery:
`[7] feature not supported: unsupported form of subquery with UNNEST`. Specifically:
- `IN (SELECT … FROM <unnest view> …)` is refused.
- `EXISTS` over a **double-UNNEST** edge-element view (link array UNNEST, then attribute list UNNEST),
  correlated to an outer UNNEST view, is refused.
- `EXISTS` over a **single-UNNEST** node-element view is **accepted**.

## Actual (from repro.py)
```
EXISTS over single-UNNEST (node) -> OK
IN (subquery over UNNEST view)   -> [7] unsupported form of subquery with UNNEST
EXISTS over double-UNNEST (edge) -> [7] unsupported form of subquery with UNNEST
```

## How to judge
A documented limitation on UNNEST inside subqueries would make this bucket B. (A2/H2 is the
**crash** variant of the same fragility: a correlated scalar subquery over an UNNEST view aborts with
code 3 rather than a clean code-7 rejection — that one is bucket A.)

## What the provider does
Drops the doc `frontier` path form, and withholds the doc view variants for an edge LIST-membership
(`concrete_syntax/path.py`; `select._views_ask`). The collection's own `FOR ANY` filter covers the
list-membership shape.
