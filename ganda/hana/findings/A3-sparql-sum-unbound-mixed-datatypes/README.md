# A3 / H3 — SPARQL SUM / AVG turn unbound once a predicate has held a second datatype

**Bucket:** A (candidate HANA bug — wrong answer). **Version:** HANA Cloud 4.00.000.00.
**Repro:** `python3 -I repro.py` (a fresh IRI namespace per run)

## Claim
Over a group in which the aggregated variable is unbound for some solutions, `SUM` and `AVG` skip
the unbound value while the predicate IRI has held one datatype; once it has held a second datatype
**anywhere in the store** (another class, another graph, a graph since dropped), the same query over
the same matching triples answers `SUM` / `AVG` UNBOUND. `MAX` and `COUNT` skip the value either way.

## Steps to reproduce
Run each request with `CALL SYS.SPARQL_EXECUTE(<request>, 'Accept: application/sparql-results+json', ?, ?)`
(an update with `''` as the second argument). HANA remembers every datatype a predicate has held,
even after its graph is dropped, so use a FRESH namespace each time (`urn:a3:` -> `urn:a3b:` ...).

1. Insert three resources of class `c:n`; two have an `xsd:long` value, one has none:
   ```sparql
   PREFIX p: <urn:a3:p:> PREFIX c: <urn:a3:c:> PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
   INSERT DATA { GRAPH <urn:a3:g> {
     <urn:a3:a> a c:n ; p:i "3"^^xsd:long .
     <urn:a3:b> a c:n ; p:i "4"^^xsd:long .
     <urn:a3:c> a c:n .
   } }
   ```
2. Aggregate the value, which is unbound for `<urn:a3:c>`:
   ```sparql
   PREFIX p: <urn:a3:p:> PREFIX c: <urn:a3:c:>
   SELECT (SUM(?w) AS ?sum) (AVG(?w) AS ?avg) (MAX(?w) AS ?max) (COUNT(?w) AS ?count)
   WHERE { GRAPH <urn:a3:g> { ?n a c:n . OPTIONAL { ?n p:i ?w } } }
   ```
   -> sum 7, avg 3.5, max 4, count 2 (correct).
3. Insert one unrelated triple: a resource of ANOTHER class whose `p:i` is a string:
   ```sparql
   PREFIX p: <urn:a3:p:> PREFIX c: <urn:a3:c:>
   INSERT DATA { GRAPH <urn:a3:g> { <urn:a3:x> a c:other ; p:i "text" . } }
   ```
4. Run the query of step 2 again -> max 4, count 2; **sum and avg unbound** (bug).
5. Controls, both still sum 7:
   ```sparql
   PREFIX p: <urn:a3:p:> PREFIX c: <urn:a3:c:>
   SELECT (SUM(?w) AS ?sum) WHERE { GRAPH <urn:a3:g> { ?n a c:n ; p:i ?w . } }

   PREFIX p: <urn:a3:p:> PREFIX c: <urn:a3:c:>
   SELECT (SUM(COALESCE(?w, 0)) AS ?sum) WHERE { GRAPH <urn:a3:g> { ?n a c:n . OPTIONAL { ?n p:i ?w } } }
   ```
6. Clean up: `DROP SILENT GRAPH <urn:a3:g>` (the predicate's datatype memory stays).

Reproduced on 4.00.000.00.1788856605 (2026-10-23).

## Expected
The answer does not depend on triples the query never matches: sum 7, avg 3.5 before and after.

## Actual (from repro.py)
```
p:i holds xsd:long only : sum 7, avg 3.5, max 4, count 2
p:i also holds a string : max 4, count 2        (sum, avg unbound)
value bound where present / SUM(COALESCE(?w, 0)) : sum 7
```

## How to judge
SPARQL 1.1 leaves room on whether an unbound value inside `SUM` is skipped or poisons the group, but
not for the result to change with data elsewhere in the store (or already deleted). Confirm with
`../../issues-draft/H3_repro.py`, which also shows the cross-graph and after-drop cases.

## What the provider does
Keeps the unbound value out of the aggregate: a select `sum` / `avg` binds the value as a required
triple, a grouped `sum` adds `COALESCE(?a, 0)` (`concrete_syntax/select.py`, `traversal.guarded`);
probe fact K20 checks both.
