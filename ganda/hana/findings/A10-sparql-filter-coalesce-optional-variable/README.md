# A10 — SPARQL: a FILTER over COALESCE of an OPTIONAL variable keeps or drops the wrong solutions

**Bucket:** A (candidate HANA bug — wrong answer, silent). **Version:** HANA Cloud
4.00.000.00.1788856605. **Repro:** `python3 repro.py`
**Found by:** the Ganda SAP HANA probe (fact K26), 2026-10-12, as a MINUS partition that removed
every solution; narrowed to the FILTER on 2026-10-13.

## Claim
A `FILTER` that applies `COALESCE` to a variable bound only by an `OPTIONAL` keeps or drops the
wrong solutions. The same expression is right as a SELECT expression, through
`BIND ((expr) AS ?t) FILTER (?t)`, and when the variable is required.

## Steps to reproduce
Run each request with `CALL SYS.SPARQL_EXECUTE(<request>, 'Accept: application/sparql-results+json', ?, ?)`
(an update with `''` as the second argument).

1. Insert `x` with `i = 1` and `y` with no `i`:
   ```sparql
   PREFIX p: <urn:ex10:p:> PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
   INSERT DATA { GRAPH <urn:ex10:g> { <urn:ex10:x> p:id "x" ; p:i "1"^^xsd:long . <urn:ex10:y> p:id "y" . } }
   ```
2. Select the expressions as values -> `(x, 1, false), (y, 0, true)` (correct):
   ```sparql
   PREFIX p: <urn:ex10:p:>
   SELECT ?id (COALESCE(?i, 0) AS ?a) (COALESCE(?i > 1, true) AS ?b)
   WHERE { GRAPH <urn:ex10:g> { ?n p:id ?id . OPTIONAL { ?n p:i ?i } } } ORDER BY ?id
   ```
3. Filter on the first -> **`x, y`** (bug; expected `y`, the only row where `?a` is 0):
   ```sparql
   PREFIX p: <urn:ex10:p:>
   SELECT ?id WHERE { GRAPH <urn:ex10:g> { ?n p:id ?id . OPTIONAL { ?n p:i ?i } FILTER (COALESCE(?i, 0) = 0) } }
   ```
4. Filter on the second -> **`x, y`** (bug; expected `y`, the only row where `?b` is true):
   ```sparql
   PREFIX p: <urn:ex10:p:>
   SELECT ?id WHERE { GRAPH <urn:ex10:g> { ?n p:id ?id . OPTIONAL { ?n p:i ?i } FILTER (COALESCE(?i > 1, true)) } }
   ```
5. Control: the same expression through BIND -> `y` (correct):
   ```sparql
   PREFIX p: <urn:ex10:p:>
   SELECT ?id WHERE { GRAPH <urn:ex10:g> { ?n p:id ?id . OPTIONAL { ?n p:i ?i } BIND ((COALESCE(?i, 0) = 0) AS ?t) FILTER (?t) } }
   ```
6. Clean up: `DROP SILENT GRAPH <urn:ex10:g>`

## Actual (`repro.py`, 4.00.000.00.1788856605, 2026-10-23)
```
expression values: [('x', '1', 'false'), ('y', '0', 'true')]

COALESCE(?i, 0) = 0                 FILTER: ['x', 'y']   BIND + FILTER: ['y']   expected ['y']
COALESCE(?i > 1, true)              FILTER: ['x', 'y']   BIND + FILTER: ['y']   expected ['y']
COALESCE(IF(?i > 1, 1, 0), 2) = 2   FILTER: ['x', 'y']   BIND + FILTER: ['y']   expected ['y']

required ?i: FILTER (COALESCE(?i > 1, true))   []   expected []
every solution MINUS the COALESCE partition    FILTER: []   BIND: ['x']   expected ['x']
```
On 4.00.000.00.1788859455 (2026-10-13, `repro-output.txt`) step 3 answered `[]` instead: the
FILTER was wrong on both builds, in opposite directions. Every other line is unchanged.

## How to judge
SPARQL 1.1 §17.4.1.4: `COALESCE` returns its first argument that evaluates without error, so for
`y` (no `i`) `COALESCE(?i, 0)` is 0 and `COALESCE(?i > 1, true)` is true; for `x` they are 1 and
false. A FILTER keeps exactly the solutions whose expression is true (§17.4), so both filters
answer `y`. HANA's own SELECT and BIND forms give those values; only the FILTER disagrees.

## What the provider does
See `HANA-bugs-A1-A10-summary.md`, section A10.
