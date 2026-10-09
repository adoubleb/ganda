# B10 — internal error for a FILTER naming a variable bound nowhere

**Bucket:** B/A boundary (you decide). **Repro:** `python3 -I repro.py`

## Claim
A SPARQL FILTER that references a variable appearing in **no** triple pattern of the group aborts the
whole statement with `[129] … FILTER(s) contains unbound variables: Cannot resolve for variables -
?undeclared`. Declaring that variable (so it exists in the pattern) while leaving it unbound per
solution — via a never-matching `OPTIONAL { ?n <urn:ganda:never> ?undeclared }` — yields a clean
result.

## Actual (from repro.py)
```
FILTER names an undeclared var   -> [129] ... FILTER(s) contains unbound variables ...
same, var declared (never bound) -> OK  []
```

## How to judge
SPARQL 1.1 treats an unbound variable in a FILTER expression as a per-solution **type error** (that
solution's effective boolean value is error → the row is excluded), not a whole-query failure.
HANA's static "the variable is bound nowhere → internal error" is arguably non-conforming — leaning
toward a bug — but it is a static-analysis choice rather than a wrong answer, so flagged on the
boundary. (The provider only needs this for the `native` error-propagation variants, which
deliberately lean on SPARQL's error semantics.)

## What the provider does
When a native filter uses its never-bound sentinel `?ganda_unknown`, it declares it with a
never-matching OPTIONAL so the FILTER parses, while the variable stays unbound so error propagation
excludes the row (`concrete_syntax/predicate.py`, `sparql_filter`).
