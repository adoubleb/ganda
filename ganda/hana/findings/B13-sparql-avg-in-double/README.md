# B13 — SPARQL averages longs in DOUBLE; SQL's AVG over BIGINT is DECIMAL(25,6)

**Bucket:** B (a departure from SPARQL 1.1 that refuses nothing and crashes nothing; within
one database two engines average the same integers differently). **Repro:** `python3 -I repro.py`
**Found by:** the Ganda SAP HANA fuzzer, the 12-hour campaign of 2026-10-12 (`camp12h-16` and
`camp12h-5`, two `select agg:avg` mismatches between the rdf data model and the others).

## Claim
Over a group of `xsd:long` values the Knowledge Graph Engine's `AVG(?w)`, and `SUM(?w) /
COUNT(?w)`, answer `double(SUM) / COUNT`: the sum is converted to a double before the
division, so a sum above 2^53 loses its low bits, and the quotient keeps a double's
seventeen digits -- not SPARQL 1.1's exact `xsd:decimal`. HANA SQL's `AVG` over the same
values in a BIGINT column answers `DECIMAL(25,6)` (documented, "AVG Function"), the exact
quotient truncated to six places.

| values | exact mean | SQL `AVG(BIGINT)` | SPARQL `AVG(?w)`, `SUM / COUNT` |
|---|---|---|---|
| -5479 and 28 zeros | -188.9310344827586206... | `-188.931034` | `-188.93103448275863` |
| 7 and 127 zeros | 0.0546875 | `0.054687` (truncated, not rounded) | `0.0546875` |
| 608, 608, 9007199254740993 (the campaign's group) | 3002399751580736.333... | `3002399751580736.333333` | `3002399751580736` (double(9007199254742209) is 9007199254742208) |

## Actual (`repro.py`, 2026-10-12)
```
HANA version: 4.00.000.00.1788859455 

-5479 / 29  
  SPARQL AVG(?w)               : -188.93103448275863
  SPARQL SUM(?w) / COUNT(?w)   : -188.93103448275863
  SPARQL IF(COUNT > 0, AVG, MAX) : -188.93103448275863
  SQL    AVG(w) DECIMAL(25,6)  : -188.931034
7 / 128     
  SPARQL AVG(?w)               : 0.0546875
  SPARQL SUM(?w) / COUNT(?w)   : 0.0546875
  SPARQL IF(COUNT > 0, AVG, MAX) : 0.0546875
  SQL    AVG(w) DECIMAL(25,6)  : 0.054687
9007199254742209 / 3 (a sum above 2^53)
  SPARQL AVG(?w)               : 3002399751580736
  SPARQL SUM(?w) / COUNT(?w)   : 3002399751580736
  SPARQL IF(COUNT > 0, AVG, MAX) : 3002399751580736
  SQL    AVG(w) DECIMAL(25,6)  : 3002399751580736.333333

  => the exact means are -188.931034482758620689..., 0.0546875 and 3002399751580736.333...: SQL answers
     the DECIMAL(25,6) truncation (-188.931034, 0.054687, 3002399751580736.333333); the triple store answers
     double(SUM) / COUNT (-188.93103448275863, 0.0546875, and 3002399751580736: the sum's low bits lost).
```

## How to judge
- SPARQL 1.1 §17.4: `Avg` is `Sum / Count`, and the division of two `xsd:integer` values is
  `xsd:decimal` (XPath `op:numeric-divide`), so the mean of integers is exact. The engine
  converts the sum to a double first (the campaign saw `3002399751580736` where SQL answered
  `...736.333333`: its sum 9007199254742209 is 1217 above 2^53, not a double)
  and divides in double.
- SQL's `DECIMAL(25,6)` is documented; that it truncates rather than rounds the seventh
  place (`0.0546875` to `0.054687`) is the engine's choice, recorded by probe R20.
- Impact: the same average asked through SQL and through SPARQL over one database's data
  differs in its last digits, and for large sums in its integer part.

## What the provider does
The reference computes `avg` over integers as SQL does (`semantics.mean`: the quotient
truncated to six places); an rdf `avg` variant is compared only where double arithmetic and
`DECIMAL(25,6)` agree -- a sum below 2^53 and a quotient exact in six places
(`known_bugs.B13`). Probe K23 records the behaviour; `RULES.md`, "Query forms".
