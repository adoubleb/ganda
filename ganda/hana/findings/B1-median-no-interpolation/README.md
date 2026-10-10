# B1 — SQL MEDIAN over integers answers an integer (the rounded mean of the middles)

**Bucket:** B — documented behaviour, not a bug (verified 2026-10-10). **Repro:** `python3 -I repro.py`

## Claim (corrected 2026-10-10)
`MEDIAN` answers in the argument's type. Over an even count of integers it takes the mean of the two
middle values and ROUNDS it to an integer, half away from zero: `{1,2}` → 2, `{1,10}` → 6,
`{-3,-2}` → −3. DECIMAL and DOUBLE keep the fraction (`{1,10}` → 5.5). The first write-up said
"the upper of the two middle values"; `{1,10}` → 6 (not 10) shows it is the rounded mean.

## Actual (from repro.py)
```
MEDIAN of BIGINT {1,2}      -> 2       MEDIAN of BIGINT {1,10}   -> 6
MEDIAN of BIGINT {1,2,3,10} -> 3       MEDIAN of BIGINT {-3,-2}  -> -3
MEDIAN of DOUBLE {1,10}     -> 5.5     PERCENTILE_CONT(0.5) {1,10} -> 5.5
```

## Verdict
**Documented.** The SQL Reference's MEDIAN page: for an even count "the average of the two middle
elements is returned", in the type selected for `x/2`; its own example over INT {1,2,3,4} says the
integer is rounded and returns 3. (https://help.sap.com/docs/hana-cloud-database/sap-hana-cloud-sap-hana-database-sql-reference-guide/median-function-aggregate)

## What the provider does
Renders the statistical median as `PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY f)`, which
interpolates whatever the type (`concrete_syntax/select.py`).
