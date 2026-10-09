# B5 — doc UPDATE: no combined SET/UNSET, SET takes only constants

**Bucket:** B (quirk). **Repro:** `python3 -I repro.py`

## Claim
Two restrictions on a Document Store `UPDATE`:
1. A combined `UPDATE c SET … UNSET …` is a syntax error (`257`), though `SET …` alone and
   `UNSET …` alone each work.
2. A `SET` expression must be constant: `$."f" = $."f" + c` (reading the field) is refused
   (`[7] only constant expressions allowed`).

## Actual (from repro.py)
```
combined SET ... UNSET       -> [257] incorrect syntax near "UNSET"
SET alone                    -> OK
UNSET alone                  -> OK
SET f = f + c (field arith)  -> [7] only constant expressions allowed
```

## How to judge
Grammar restrictions of the collection DML — check the Document Store Guide's UPDATE syntax. Not
wrong answers; bucket B.

## What the provider does
Emits `SET` and `UNSET` as separate statements (`mapping/document.py`, `_field_update`), and computes
an arithmetic write's new value in Python per row, binding a constant (`concrete_syntax/write.py`,
the `arith` step) — never `f = f + c`.
