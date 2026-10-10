# H3 -- SPARQL SUM / AVG turn unbound over a group with an unbound value once the predicate has held a second datatype anywhere in the store

**Version:** SAP HANA Cloud, `SYS.M_DATABASE.VERSION` = `4.00.000.00.1788859455` (2026-10), Knowledge
Graph Engine (triple store) reached through `SYS.SPARQL_EXECUTE`.
**Client:** `hdbcli`, `CALL SYS.SPARQL_EXECUTE(?, 'Accept: application/sparql-results+json', ?, ?)`.
**Found by:** the Ganda SAP HANA fuzzer's 12-hour campaign `campaign-20261009-165805`, as 9
mismatches: every `rdf` variant of a select `sum` / `avg` (and of a grouped `sum`) answered unbound
where the column-table and Document Store variants, and the reference, answered the sum.

## Summary

Over a group of solutions in which the aggregated variable is unbound for some solutions (the usual
`OPTIONAL { ?n p:i ?w }` pattern), `SUM(?w)` and `AVG(?w)` skip the unbound value -- **until the
predicate IRI `p:i` has held a value of a second datatype anywhere in the triple store**. From then
on, the same query over the same matching triples answers UNBOUND for `SUM` and `AVG`. The second
datatype may sit on a resource the query never matches, in another named graph, and in a graph
that has since been dropped: the effect stays. `MAX` and `COUNT` skip the unbound value either way.

## Steps to reproduce

```sparql
# <NS> is a fresh IRI prefix: HANA remembers every datatype a predicate IRI has held, so a
# second run must not reuse the IRIs of a first one
PREFIX p: <NS:p:> PREFIX c: <NS:c:> PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>

# three resources of class c:n: two with p:i (3, 4), one without
INSERT DATA { GRAPH <NS:g> { <NS:a> a c:n ; p:i "3"^^xsd:long .
                             <NS:b> a c:n ; p:i "4"^^xsd:long .
                             <NS:c> a c:n . } }

# (1) the aggregates: sum 7, avg 3.5, max 4, count 2 -- as expected
SELECT (SUM(?w) AS ?sum) (AVG(?w) AS ?avg) (MAX(?w) AS ?max) (COUNT(?w) AS ?count)
WHERE { GRAPH <NS:g> { ?n a c:n . OPTIONAL { ?n p:i ?w } } }

# one resource of ANOTHER class gives p:i a second datatype
INSERT DATA { GRAPH <NS:g> { <NS:x> a c:other ; p:i "text" . } }

# (2) the same query as (1): max 4, count 2 -- sum and avg are now unbound
```

## Expected vs actual

- **Expected:** query (2) answers what query (1) answered (sum 7, avg 3.5): it matches exactly the
  same triples, and the inserted triple is of a class the query does not read. Whatever the reading
  of SPARQL 1.1 on an unbound value inside `SUM` (skipped, as HANA does at first and as `MAX` /
  `COUNT` do, or an error that makes the aggregate unbound), it must not depend on data the query
  never touches.
- **Actual:** query (2) answers `max 4, count 2` with `sum` and `avg` unbound.

## Characterisation (each bullet an isolated trial, `H3_repro.py` and the Ganda probes)

- The trigger is the predicate's datatype history across the store, not the graph queried: a string
  on `p:i` in a *different* named graph flips the answer; dropping that graph does not flip it back;
  dropping and re-creating the queried graph does not either.
- With a single datatype on the predicate, a group mixing bound and unbound values sums the bound
  ones (7). After the second datatype appears, the same group answers unbound. A group whose values
  are all bound still sums correctly (`9007199254740993 + 123456789012345678`); a group with no bound
  value is unbound either way (correct).
- Tried second datatypes: an `xsd:string`; an `xsd:string`, an `xsd:double` and an `xsd:boolean`
  together. Each triggers it.
- `MAX` and `COUNT` over the same group are unaffected; `SUM` and `AVG` both are. An `IF(COUNT(?w) >
  0, SUM(?w), ...)` around the aggregate does not help (it is the `SUM` itself that is unbound).
- `SUM` over the value bound as a required triple (`?n a c:n ; p:i ?w .`) and `SUM(COALESCE(?w, 0))`
  both answer 7 after the second datatype appears.

## Impact

A SPARQL `SUM` / `AVG` over an optional property -- "the total of `p:price` over the products, some
of which have none" -- silently returns nothing as soon as any resource anywhere in the store ever
stored that property with another datatype (a string price, a typo in a load). The answer of a
query changes because of data in other graphs, including data already deleted, with no error.
Through `SPARQL_TABLE` the same aggregate comes back as SQL NULL.

## Workaround

Keep the unbound value out of the aggregate: bind the value as a required triple when the group is
the whole result, or sum `COALESCE(?w, 0)` when the groups must keep their records. The Ganda HANA
provider does both (`concrete_syntax/select.py`, `traversal.guarded`); probe fact K20 checks them.

## Repro script

`H3_repro.py` in this folder is a self-contained reproduction (needs only `hdbcli`, connects from
`HANA_*`, uses a fresh IRI namespace per run); `H3_repro_output.txt` is its transcript against HANA
Cloud 4.00.000.00.
