# A9 — a second OPENCYPHER_TABLE call reads the first call's parameter values

**Bucket:** A (candidate engine defect: a statement whose calls each answer correctly alone
returns wrong rows, or fails, once two calls share it).
**Version:** HANA Cloud 4.00.000.00.1788859455. **Repro:** `python3 repro.py`
**Found by:** the Ganda SAP HANA fuzzer, the 12-hour campaign of 2026-10-12 (bastion run
`camp12h`). It showed up as seven error asymmetries on the graph data model's traversal
texts (a `starts` call LEFT JOINed to a `paths` call) and three silent mismatches. The cause
was established live on 2026-10-13.

## Claim
In one SQL statement, every `OPENCYPHER_TABLE` call numbers its `PARAMETERS` from 1 against
the statement's whole list of `?`. The second call's k-th parameter therefore reads the
statement's k-th `?`, which holds the first call's value, and the second call's own `?` is
ignored. The borrowed value is converted to the type of the parameter that reads it.

```
SELECT ID FROM OPENCYPHER_TABLE(GRAPH WORKSPACE w QUERY 'MATCH (n) WHERE n.I = $p RETURN n.ID AS ID' PARAMETERS ('p' = ?))
UNION ALL
SELECT ID FROM OPENCYPHER_TABLE(GRAPH WORKSPACE w QUERY 'MATCH (n) WHERE n.I = $q RETURN n.ID AS ID' PARAMETERS ('q' = ?))
    with (1, 2)       -> [('a',), ('a',)]      expected [('a',), ('b',)]
```

## Actual (`repro.py`, 2026-10-13)
```
  one call, binds (1)                         : OK [('a',)]                 expected [('a',)]
  one call, binds (2)                         : OK [('b',)]                 expected [('b',)]
  two calls UNION ALL, binds (1, 2)           : OK [('a',), ('a',)]         expected [('a',), ('b',)]
  SQL ? then a call, binds ('z', 2)           : [339] invalid number: not a valid number string 'z'   expected [('z',), ('b',)]
```

Further live checks (2026-10-13):
- Two NVARCHAR calls behave the same way: binds `('x', 'y')` return `a, a`.
- Naming both parameters `p` changes nothing.
- A two-parameter call followed by a one-parameter call reads the first `?` again.
- When the types differ, the borrowed value is converted: a string read by a BIGINT
  parameter fails (`[339]`, or `[-10427]` from the client), and a DOUBLE `1.5` read by a
  BIGINT parameter becomes `1` and silently matches the wrong rows.

## How the campaign's observations follow
- **The traversal text** bound `('o', 'z', 'o', 'z', -23)`: starts `(p0a, p0b)`, paths
  `(p0a, p0b, p1)`. The paths call's third parameter `p1` (BIGINT) read the statement's third
  `?`, the string `'o'`, so the client failed converting it: `[-10427] Conversion of
  parameter/column (3) ... to BIGINT failed`.
- **With `p1` declared first,** it read `?1 = 'o'`: `[339] invalid number`.
- **With every parameter a string,** or with the strings written as constants, the borrowed
  values happened to be right (the same pivots in the same positions), so the statement
  answered correctly.
- **The three silent mismatches** (`camp12h-2 ... distinct_count`, `camp12h-11 ... nexists`,
  `camp12h-17 ... emax`) are a DOUBLE read by a BIGINT parameter.

## How to judge
- Each call alone binds its parameters correctly. The texts use the documented form
  (Property Graph Engine Reference, QRC 2/2026, `OPENCYPHER_TABLE ... PARAMETERS`), and SQL
  binds `?` in order of appearance. A table function that reads another call's `?` breaks
  that.
- Impact: any statement that joins or unions two parameterised calls, or that has a SQL `?`
  before a call, can silently return wrong rows.

## What the provider does
It binds one parameter type per call: a DOUBLE pivot is bound, and every other pivot is a
constant in the cypher text (`graph.cypher_filter`; probe G32). This avoids the type errors
but not the positional reading. Two calls that bind different DOUBLEs still read the wrong
value. The renderer needs an audit (inline doubles as `TOFLOAT('<repr>')`, which is exact per
G31, or bind only in single-call statements).
