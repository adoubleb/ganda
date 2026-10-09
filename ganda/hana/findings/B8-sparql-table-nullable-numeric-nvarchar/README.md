# B8 — SPARQL_TABLE types a maybe-unbound numeric variable as NVARCHAR

**Bucket:** B (quirk). **Repro:** `python3 -I repro.py`

## Claim
The SAME SPARQL query returns an integer field with its datatype when run **directly** through
`SYS.SPARQL_EXECUTE` (binding carries `xsd:long`), but as an **NVARCHAR** column (`'7'`) when wrapped
in `SPARQL_TABLE`. So `SPARQL_TABLE` drops the datatype of a maybe-unbound (OPTIONAL) numeric
variable and widens the column to NVARCHAR.

## Actual (from repro.py)
```
direct SPARQL_EXECUTE binding : [None, {datatype xsd:long, value '7'}]   -> decodes to int 7
SPARQL_TABLE column type code 11 (NVARCHAR), rows [(None,), ('7',)]      -> string '7'
```

## How to judge
If SAP documents that SPARQL_TABLE widens a nullable column to NVARCHAR, it is a quirk. It is
defensible (a nullable column needs a common SQL type), so bucket B — but the inconsistency with the
direct SPARQL path is the thing to confirm you are comfortable calling a quirk.

## What the provider does
Aliases such a projection in the SPARQL text with its decode suffix (`?a AS ?a__int` / `__float`) so
the session re-types the string column (`concrete_syntax/select.py` `_sparql_proj`,
`traversal.py` `_rdf_proj`).
