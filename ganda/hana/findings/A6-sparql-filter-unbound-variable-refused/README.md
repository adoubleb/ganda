# A6 / H6 — SPARQL: FILTER / BIND over a variable bound nowhere is refused

**Bucket:** A (conformance bug — a legal query refused; the answers HANA gives are right).
**Version:** HANA Cloud 4.00.000.00. **Repro:** `python3 -I repro.py` (was B10, "B/A boundary")

## Claim
A query whose FILTER names a variable that no triple pattern binds fails as a whole (`[129] ...
FILTER(s) contains unbound variables`), as does a BIND of it -- even `FILTER(!BOUND(?x))`, which the
standard makes TRUE for every solution. `COALESCE(?x, 1)` is accepted; naming `?x` in a
never-matching OPTIONAL makes every form accepted, with the standard's answers.

## Actual (from repro.py)
```
FILTER(!BOUND(?x))          : [129] FILTER(s) contains unbound variables   (standard: a, b)
FILTER(?x = 1)              : [129] FILTER(s) contains unbound variables   (standard: none)
BIND(?x AS ?y)              : [129] Named variable not in contained WHERE clause (standard: a, b)
FILTER(COALESCE(?x, 1) = 1) : a, b
OPTIONAL names ?x, !BOUND   : a, b
```

## How to judge (verified 2026-10-10)
W3C SPARQL 1.1: §18.2.1 allows a FILTER over a variable not in scope; an unbound variable is a
per-solution type error (§17.2) that drops the solution (§17); BOUND never raises. SAP's SPARQL
Reference does not list the restriction. Since every SPARQL refusal is code 129 (a syntax error too),
the code says nothing about an internal failure; the defect is the refusal of a legal query.

## What the provider does
Declares its never-bound `?ganda_unknown` with a never-matching OPTIONAL (`concrete_syntax/predicate.py`,
`sparql_filter`). Write-up: `../../issues-draft/H6-sparql-filter-or-bind-on-a-variable-bound-nowhere-is-refused.md`.
