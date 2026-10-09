# B1 — MEDIAN does not interpolate an even count of integers

**Bucket:** B (quirk; verify against SAP's MEDIAN docs). **Repro:** `python3 -I repro.py`

## Claim
`MEDIAN` of an even count of **integers** returns the upper of the two middle values (e.g. `{1,2}` →
`2`), not the interpolated `1.5`. `MEDIAN` of **doubles** interpolates (`{1.0,2.0}` → `1.5`). So MEDIAN
is effectively type-dependent: `PERCENTILE_DISC`-like on exact types, `PERCENTILE_CONT`-like on
approximate ones.

## Actual (from repro.py)
```
MEDIAN of INT {1,2}        -> 2       (not 1.5)
MEDIAN of INT {1,2,3,4}    -> 3       (not 2.5)
MEDIAN of DOUBLE {1.0,2.0} -> 1.5
PERCENTILE_CONT(0.5) INT   -> 1.5
```

## How to judge
Check the SAP HANA SQL Reference for `MEDIAN`: if it documents returning an actual data value (not
interpolating) for exact numeric types, this is a documented quirk. The statistical median most tools
return is the interpolating one, which is `PERCENTILE_CONT(0.5)`.

## What the provider does
Renders the oracle's statistical median as `PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY f)` so it
matches the interpolating reference regardless of column type (`concrete_syntax/select.py`).
