# A1 / H1 — SPARQL serialises an `xsd:double` to ~6 significant figures

**Bucket:** A (candidate HANA bug). **Version:** HANA Cloud 4.00.000.00. **Repro:** `python3 -I repro.py`

## Claim
An `xsd:double` is stored exactly but every SPARQL result format emits the numeric term rounded to
~6 significant figures. `STR(?d)` on the same binding returns the full 17-digit lexical form, so the
loss is in the result serialiser, not storage or evaluation.

## Expected
A round trip of a double returns the same IEEE-754 value. SPARQL 1.1 result formats carry the
literal's lexical form; a conforming serialiser emits a form that maps back to the same double
(`3.141592653589793`), which is what `STR()` yields.

## Actual (from repro.py)
```
?d term          -> "3.14159"                 (datatype xsd:double)
STR(?d)          -> "3.1415926535897931"      (exact)
?d * 1000000000  -> "3.14159e+09"             (rounded too)
```
`0.30000000000000004` → `0.3`, `1.7976931348623157e+308` → `1.79769e+308`, etc.

## How to judge
Confirm the stored value is intact (`STR(?d)`), then decide whether a result serialiser that loses
double precision is a defect. Six significant figures is the C `printf("%g")` default, which suggests
`%g` formatting rather than a round-trip (`%.17g` / shortest) format.

## What the provider does
Reads every rdf double through `STR(?v)` and parses it (`connection._rdf_records`, the `__float`
projections of the case queries). Without the workaround the rdf data model cannot round-trip a
fractional number, so the oracle sees a spurious mismatch against the rel/doc models.
