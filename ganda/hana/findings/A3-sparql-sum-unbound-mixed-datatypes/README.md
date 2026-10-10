# A3 / H3 — SPARQL SUM / AVG turn unbound once a predicate has held a second datatype

**Bucket:** A (candidate HANA bug — wrong answer). **Version:** HANA Cloud 4.00.000.00.
**Repro:** `python3 -I repro.py` (a fresh IRI namespace per run)

## Claim
Over a group in which the aggregated variable is unbound for some solutions, `SUM` and `AVG` skip
the unbound value while the predicate IRI has held one datatype; once it has held a second datatype
**anywhere in the store** (another class, another graph, a graph since dropped), the same query over
the same matching triples answers `SUM` / `AVG` UNBOUND. `MAX` and `COUNT` skip the value either way.

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
