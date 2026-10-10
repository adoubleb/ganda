# H6 -- SPARQL: a legal FILTER or BIND over a variable no pattern binds is refused

**Version:** SAP HANA Cloud, `SYS.M_DATABASE.VERSION` = `4.00.000.00.1788859455` (2026-10), Knowledge
Graph Engine (triple store) reached through `SYS.SPARQL_EXECUTE`.
**Client:** `hdbcli`, `CALL SYS.SPARQL_EXECUTE(?, 'Accept: application/sparql-results+json', ?, ?)`.
**Found by:** the Ganda SAP HANA probe (fact K6), on the fuzzer's `native` filter renderings, which
leave a semi-join's unknown to SPARQL's own error propagation through a never-bound variable (filed
then as B10, "B/A boundary"). Confirmed against the W3C specification and SAP's docs 2026-10-10.

## Summary

A query whose `FILTER` names a variable that no triple pattern binds is refused as a whole --
`[129] ... FILTER(s) contains unbound variables: Cannot resolve for variables - ?x` -- and so is a
`BIND` of such a variable (`?x: Named variable not in contained WHERE clause`). This includes
`FILTER(!BOUND(?x))`, whose answer SPARQL 1.1 fixes as TRUE for every solution. The same variable
inside `COALESCE(?x, 1)` is accepted, and so is the `FILTER` once an `OPTIONAL` that never matches
names the variable.

## Steps to reproduce

```sparql
PREFIX p: <NS:p:>
INSERT DATA { GRAPH <NS:g> { <NS:a> p:id "a" . <NS:b> p:id "b" . } }

SELECT ?id WHERE { GRAPH <NS:g> { ?n p:id ?id . FILTER(!BOUND(?x)) } }
--   [129] ... FILTER(s) contains unbound variables: Cannot resolve for variables - ?x
--   expected: a, b

SELECT ?id WHERE { GRAPH <NS:g> { ?n p:id ?id . FILTER(COALESCE(?x, 1) = 1) } }
--   OK: a, b

SELECT ?id WHERE { GRAPH <NS:g> { ?n p:id ?id . OPTIONAL { ?n <NS:never> ?x } FILTER(!BOUND(?x)) } }
--   OK: a, b
```

## Expected vs actual

- **Expected** (W3C SPARQL 1.1): the query is legal. A variable that no pattern binds is unbound in
  every solution; §18.2.1 allows a FILTER over a variable that is not in scope; `BOUND` never raises
  an error, so `FILTER(!BOUND(?x))` keeps every solution and `FILTER(BOUND(?x))` none; an expression
  that evaluates an unbound variable raises a type error (§17.2), and a FILTER whose expression errors
  drops that solution (§17), so `FILTER(?x = 1)` answers no row; `BIND(?x AS ?y)` leaves `?y` unbound.
- **Actual:** each of `FILTER(!BOUND(?x))`, `FILTER(BOUND(?x))`, `FILTER(?x = 1)` and
  `BIND(?x AS ?y)` aborts the statement with error 129.

## Characterisation (each bullet an isolated trial, `H6_repro.py` and the Ganda probes)

- The refusal is static: it does not depend on the data, and it fires for `BOUND`, which cannot
  raise an error at all.
- `COALESCE(?x, 1)` over the same never-bound variable is accepted and answers per the standard.
- Naming the variable in a never-matching `OPTIONAL` makes every form accepted, with the
  standard's answers: the variable is no less unbound, only declared.
- SAP's SPARQL Reference does not list the restriction (searched 2026-10-10: the WHERE clause,
  FILTER, BIND and BOUND pages).

## Impact

Generated SPARQL -- a query builder that emits a FILTER or BIND for an optional part of a template
it then leaves out -- fails as a whole rather than answering. A legal query is refused; the answers
of the accepted forms are right, so this is a conformance defect, not a wrong-result one.

## Workaround

Name the variable in an `OPTIONAL` that never matches (`OPTIONAL { ?n <urn:never> ?x }`). The Ganda
HANA provider declares its never-bound `?ganda_unknown` this way (`concrete_syntax/predicate.py`,
`sparql_filter`).

## Repro script

`H6_repro.py` in this folder is a self-contained reproduction (needs only `hdbcli`, connects from
`HANA_*`, uses a fresh IRI namespace per run); `H6_repro_output.txt` is its transcript against HANA
Cloud 4.00.000.00.
