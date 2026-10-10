# A4 / H4 — Document Store SQL: SUM over a join that matches no row overflows

**Bucket:** A (candidate HANA bug — spurious error). **Version:** HANA Cloud 4.00.000.00.
**Repro:** `python3 -I repro.py`

## Claim
A `SUM` over a join of collection views fails with `[314] numeric overflow` although the join
matches no row (the answer is NULL): it adds up values of links the join excludes (`2^63 - 1` and
`1`). The same query over column tables, or summing `TO_DECIMAL(...)`, answers NULL.

## Trigger
The link view reads the link whole (its id and far end as well as its value), and the value is cast
in a CTE of its own over the raw collection read. With the value column read alone, it answers NULL.

## Expected
`NULL`, as the column tables answer.

## Actual (from repro.py)
```
collections: the join's rows       : []
collections: SUM over the join     : [314] numeric overflow: SQL Error
collections: SUM(TO_DECIMAL(...))  : [(None,)]
collections: link view, value only : [(None,)]
column tables: SUM over the join   : [(None,)]
```

## How to judge
An overflow for a SUM over no row cannot come from the answer; it is a plan that aggregates before
(or regardless of) the join. Whether it fires depends on which columns a view reads.

## What the provider does
Withholds the doc variants of a traversal sum whose summed column holds an absolute total of `2^62`
or more (`known_bugs.py`, H4); the rel and rdf variants still answer.
