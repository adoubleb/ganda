# B8 — SPARQL_TABLE types a column after its predicate's datatype history across the store

**Bucket:** B — the core rule is documented; its scope is not (verified 2026-10-10).
**Repro:** `python3 -I repro.py` (a fresh IRI namespace per run)

## Claim (corrected 2026-10-10)
A SPARQL_TABLE result column is typed after the datatypes its PREDICATE has held across the whole
triple store: with one datatype the column is typed (INTEGER, BOOLEAN), OPTIONAL or not; once the
predicate has held a second datatype -- in another graph, even one since dropped -- the column is
NVARCHAR (`'7'`, a boolean as `'1'`). The same query through SPARQL_EXECUTE keeps `xsd:long`. The
first write-up blamed OPTIONAL; its repro used the campaign's own `p:f1`, which had long held
several datatypes.

## Actual (from repro.py)
```
p:i, p:b hold one datatype each    : types [NVARCHAR, INTEGER, BOOLEAN]  rows ('a', 7, True)
a string on each, in ANOTHER graph : types [NVARCHAR, NVARCHAR, NVARCHAR] rows ('a', '7', '1')
after that graph is dropped        : types [NVARCHAR, NVARCHAR, NVARCHAR] rows ('a', '7', '1')
```

## Verdict
**Documented in its core, not in its scope.** The SPARQL Reference's SPARQL_TABLE page: a variable
whose values have several data types becomes a string (NVARCHAR) column (https://help.sap.com/docs/hana-cloud-database/sap-hana-cloud-sap-hana-database-sparql-reference-guide/sparql-table-function).
Undocumented: that the decision rests on the predicate's datatypes anywhere in the store, including
deleted data, rather than on the values the query returns, and that a boolean becomes `'1'` /
`'0'` (its canonical form is `true` / `false`). The same store-wide history drives H3 (A3), where it
does change an answer; here it changes only a column type.

## What the provider does
Every value projection through SPARQL_TABLE carries its decode suffix (`__int`, `__bool`, `STR(...)`
`__float`), and the offline stand-in answers SPARQL_TABLE as text to keep it so.
