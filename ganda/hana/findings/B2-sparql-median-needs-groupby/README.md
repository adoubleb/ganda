# B2 — SPARQL MEDIAN requires a GROUP BY (internal error without one)

**Bucket:** B/A boundary (you decide). **Repro:** `python3 -I repro.py`

## Claim
`SELECT (MEDIAN(?w) AS ?v) WHERE { … }` with no `GROUP BY` fails with `[129] … Unsupported
functionality: MEDIAN, PERCENTILE_CONT or PERCENTILE_DISC without GROUP BY`. The same aggregate with
a `GROUP BY` on a bound constant works and interpolates correctly (`{1,2}` → `1.5`).

## Actual (from repro.py)
```
MEDIAN no GROUP BY      -> [129] ... Unsupported functionality: MEDIAN ... without GROUP BY
MEDIAN GROUP BY const   -> [{v: 2.0}]        (median of 1,2,3)
MEDIAN GROUP BY {1,2}   -> [{v: 1.5}]        (interpolates)
```

## How to judge
If SAP documents that these percentile aggregates need an explicit grouping in the SPARQL dialect,
it is a quirk. It leans toward a bug because the error is an **internal error (129)**, not a clean
"feature not supported (7)", for a query that is legal SPARQL 1.1. Compare with SQL, where
`MEDIAN(x)` without GROUP BY is accepted (see B1).

## What the provider does
Wraps the SPARQL median in a subquery grouped on a bound constant, with a `VALUES … OPTIONAL … BIND`
outer so one row is still returned over no value (`concrete_syntax/select.py`).

## Verdict (2026-10-10)
**Undocumented limitation, inconsistent with its siblings -- a doc / feature gap, not a wrong
answer.** SAP documents MEDIAN as an extension aggregate; its only example has a GROUP BY, but no page
requires one. The sibling extension aggregates VAR and MODE answer without a GROUP BY (repro), and
SPARQL 1.1 (11.1) makes an aggregate without GROUP BY one implicit group. The message names the
limitation ("Unsupported functionality"); the code 129 means nothing more, as every SPARQL refusal is
129 (a syntax error too). (https://help.sap.com/docs/hana-cloud-database/sap-hana-cloud-sap-hana-database-sparql-reference-guide/median-function-aggregate, https://help.sap.com/docs/hana-cloud-database/sap-hana-cloud-sap-hana-database-sparql-reference-guide/aggregate-functions)
