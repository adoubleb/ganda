# H1 -- SPARQL results serialize `xsd:double` to ~6 significant figures, losing precision

**Version:** SAP HANA Cloud, `SYS.M_DATABASE.VERSION` = `4.00.000.00.1788859455` (2026-10), Knowledge
Graph Engine (triple store) reached through `SYS.SPARQL_EXECUTE`.

**Client:** `hdbcli`, `CALL SYS.SPARQL_EXECUTE(?, 'Accept: application/sparql-results+json', ?, ?)`.

## Summary

A value of type `xsd:double` is stored exactly but SPARQL result serialization (`application/sparql-results+json`, `+xml`, `text/csv`) rounds to six significant figures. 

## Steps to reproduce

```sql
-- a scratch named graph; <G> is any graph IRI the user may write
CALL SYS.SPARQL_EXECUTE(
  'PREFIX p: <urn:ganda:p:> PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
   INSERT DATA { GRAPH <G> { <urn:ganda:r:a> p:d "3.141592653589793"^^xsd:double } }',
  '', ?, ?);

-- read the double back as a term: comes back 3.14159 (6 sig figs)
CALL SYS.SPARQL_EXECUTE(
  'PREFIX p: <urn:ganda:p:>
   SELECT ?d WHERE { GRAPH <G> { ?n p:d ?d } }',
  'Accept: application/sparql-results+json', ?, ?);
--  {"d":{"datatype":".../XMLSchema#double","type":"literal","value":"3.14159"}}

-- STR() of the SAME binding: the value was stored exactly, all 17 digits are there
CALL SYS.SPARQL_EXECUTE(
  'PREFIX p: <urn:ganda:p:>
   SELECT (STR(?d) AS ?d) WHERE { GRAPH <G> { ?n p:d ?d } }',
  'Accept: application/sparql-results+json', ?, ?);
--  {"d":{"type":"literal","value":"3.1415926535897931"}}
```

## Expected vs actual

- Expected: Querying an xsd:double should return the same double value that was stored. The value appears to be stored correctly, because STR(?d) returns the full value, e.g. 3.141592653589793.

- Actual: The SPARQL result output shortens xsd:double values.