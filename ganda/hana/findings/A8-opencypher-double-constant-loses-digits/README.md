# A8 — openCypher: a DOUBLE constant in the query text is parsed to about fifteen significant digits

**Bucket:** A (candidate engine defect: a numeric literal the grammar accepts does not denote
the double it spells; the same digits bound as a parameter, or through `TOFLOAT` of a string
constant, do).
**Version:** HANA Cloud 4.00.000.00.1788859455. **Repro:** `python3 repro.py`
**Found by:** the Ganda SAP HANA fuzzer, the 12-hour campaign of 2026-10-12 (bastion run
`camp12h`): ten `graph:literal` mismatches on float pivots in the first ninety minutes (the
`literal` variant inlined every constant; the base variant bound them and agreed with the
other data models and the reference).

## Claim
Over a vertex attribute `d DOUBLE` holding `-985.195` (and `0.5`), a WHERE that compares the
attribute with a DOUBLE CONSTANT one or two ulps away from the stored value matches the row:

```
MATCH (a:rv) WHERE a.RV_D = -985.1950000000002 RETURN a.ID AS ID      -> ['b']   (one ulp below -985.195)
MATCH (a:rv) WHERE a.RV_D = -985.1950000000003 RETURN a.ID AS ID      -> ['b']   (two ulps below)
MATCH (a:rv) WHERE a.RV_D = 0.5000000000000001 RETURN a.ID AS ID      -> ['h']   (one ulp above 0.5)
MATCH (a:rv) WHERE a.RV_D = 0.5000000000000002 RETURN a.ID AS ID      -> ['h']   (two ulps above)
```

while the same values bound as parameters (`a.RV_D = $p`, `PARAMETERS ('p' = ?)`), or spelt
as `TOFLOAT('-985.1950000000002')`, match nothing -- as HANA SQL's `WHERE d = ?` matches
nothing. `TOFLOAT('-985.1950000000002')` projected comes back as `-985.1950000000002`
exactly, so the string parser is exact; the numeric-literal parser is not.

## Actual (`repro.py`, 2026-10-12)
```
stored d = -985.195; one ulp below = -985.1950000000002; two ulps below = -985.1950000000003

  SQL      d = one ulp below                          : []
  SQL      d <= one ulp below                         : []
  cypher   d = $p, one ulp below                      : []
  cypher   d <= $p, one ulp below                     : []
  cypher   d <> $p, one ulp below                     : ['b', 'h']
  cypher   d = $p, two ulps below                     : []
  cypher   d = $p, exactly                            : ['b']
  cypher   d = TOFLOAT(''-985.1950000000002'')        : []
  cypher   d = -985.1950000000002 (constant)          : ['b']
  cypher   d = -985.1950000000003 (constant)          : ['b']
  cypher   d <= two ulps below as TOFLOAT             : []
  cypher   0.5 = 0.5000000000000001 (constant, 1 ulp) : ['h']
  cypher   0.5 = 0.5000000000000002 (constant, 2 ulps): ['h']
  cypher   0.5 = $p, 0.5000000000000002 (2 ulps)      : []
  cypher   TOFLOAT(''-985.1950000000002'') projected  : [-985.1950000000002]
```

## How to judge
- The Property Graph Engine Reference (QRC 2/2026) documents a floating-point literal in
  the openCypher subset and types a DOUBLE attribute as the SQL DOUBLE; a literal with
  seventeen significant digits is a valid double (every double round-trips through
  seventeen digits) and HANA SQL parses the same text exactly (`SELECT -985.1950000000002
  FROM DUMMY` keeps every digit). The engine's literal parser keeps about fifteen.
- Not a comparison tolerance: a bound parameter and `TOFLOAT` of a constant compare
  exactly at one ulp. The defect is in parsing the literal.
- Impact: a query that spells a double constant can match rows the value does not equal, or
  miss rows it equals, silently. Workaround: bind the double, or `TOFLOAT('<digits>')`.

## What the provider does
A double is never written as a constant in a cypher text: it is bound (`graph.cypher_filter`),
and the `literal` variant that inlined constants is gone (an integer, a string and a LIST's
text inline exactly: probe G30 / G34). Probe G31 records the behaviour. `RULES.md`, "The
graph data model".
