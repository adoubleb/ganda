# A6 / H6 — SPARQL rejects a valid query when a FILTER or BIND uses a variable that is never assigned a value

**Bucket:** A (conformance bug — a legal query refused; the answers HANA gives are right).
**Version:** HANA Cloud 4.00.000.00. **Repro:** `python3 -I repro.py` (was B10, "B/A boundary")

## Claim
A query whose FILTER names a variable that no triple pattern binds fails as a whole (`[129] ...
FILTER(s) contains unbound variables`), as does a BIND of it -- even `FILTER(!BOUND(?x))`, which the
standard makes TRUE for every solution. `COALESCE(?x, 1)` is accepted; naming `?x` in a
never-matching OPTIONAL makes every form accepted, with the standard's answers.

## Steps to reproduce
Run each request with `CALL SYS.SPARQL_EXECUTE(<request>, 'Accept: application/sparql-results+json', ?, ?)`
(an update with `''` as the second argument).

1. Insert two resources:
   ```sparql
   PREFIX p: <urn:a6:p:>
   INSERT DATA { GRAPH <urn:a6:g> { <urn:a6:a> p:id "a" . <urn:a6:b> p:id "b" . } }
   ```
2. FILTER on a variable no pattern binds -> **`[129] ... FILTER(s) contains unbound variables`**
   (bug; SPARQL 1.1 answers `a, b`, since `BOUND` never raises):
   ```sparql
   PREFIX p: <urn:a6:p:>
   SELECT ?id WHERE { GRAPH <urn:a6:g> { ?n p:id ?id . FILTER(!BOUND(?x)) } }
   ```
3. The same with `FILTER(?x = 1)` -> `[129]` (standard: no rows); with `BIND(?x AS ?y)` ->
   `[129] ... Named variable not in contained WHERE clause` (standard: `a, b`).
4. Controls, each -> `a, b`:
   ```sparql
   PREFIX p: <urn:a6:p:>
   SELECT ?id WHERE { GRAPH <urn:a6:g> { ?n p:id ?id . FILTER(COALESCE(?x, 1) = 1) } }

   PREFIX p: <urn:a6:p:>
   SELECT ?id WHERE { GRAPH <urn:a6:g> { ?n p:id ?id . OPTIONAL { ?n <urn:a6:never> ?x } FILTER(!BOUND(?x)) } }
   ```
5. Clean up: `DROP SILENT GRAPH <urn:a6:g>`

Reproduced on 4.00.000.00.1788856605 (2026-10-23).

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
