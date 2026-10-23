# B14 — openCypher refuses a BOOLEAN inside COUNT and in a comparison of two attributes

**Bucket:** B (an undocumented limitation that refuses cleanly: compilation error 4906; the
Property Graph Engine Reference lists the attribute types an expression takes -- INT,
BIGINT, DOUBLE, NVARCHAR, DATE, TIMESTAMP -- and BOOLEAN is not among them).
**Repro:** `python3 -I repro.py`
**Found by:** the Ganda SAP HANA fuzzer, the 12-hour campaign of 2026-10-12 (`camp12h-5 ...
group_count_2 ... bool` and `camp12h-6 ... join ... type_bool`: two error asymmetries of the
graph data model's native texts).

## Claim
Over a vertex attribute `b BOOLEAN`:

```
MATCH (a:v) RETURN a.ID AS ID, a.B AS B                      -> answers (projection)
MATCH (a:v) RETURN a.B AS K, COUNT(a.ID) AS V                -> answers (group key)
MATCH (a:v) RETURN DISTINCT a.B AS B                         -> answers
MATCH (a:v) RETURN COUNT(a.B) AS V                           -> [4906] OpenCypher compilation error:
        Expected [integer, bigint, float, string, boolean, datetime, date, null], but found invalidType
MATCH (t:v), (m:v) WHERE (m.B <> t.B) RETURN t.ID AS ID, m.ID AS V
                                                             -> [4906] OpenCypher compilation error:
        Invalid subgraph: Unexpected argument type
```

SQL's `COUNT(b)` over the same column answers. The first error message lists `boolean` among
the types it expected and still reports the attribute as `invalidType`.

## Actual (`repro.py`, 2026-10-12)
```
HANA version: 4.00.000.00.1788859455 

  BOOLEAN projected                 : OK  [('a', True), ('b', False), ('c', None), ('d', True)]
  BOOLEAN as a group key            : OK  [(True, 2), (False, 1), (None, 1)]
  DISTINCT over a BOOLEAN           : OK  [(True,), (False,), (None,)]
  COUNT over a BOOLEAN attribute    : [4906] GraphEngine: OpenCypher compilation error: Expected [integer, bigint, float, string, boolean, dateti
  COUNT over a BOOLEAN edge attr    : [4906] GraphEngine: OpenCypher compilation error: Expected [integer, bigint, float, string, boolean, dateti
  COUNT over a BIGINT (control)     : OK  [(4,)]
  two BOOLEANs compared (<>)        : [4906] GraphEngine: OpenCypher compilation error: Invalid subgraph: Unexpected argument type: line 1 col 57
  two BOOLEANs compared (=)         : [4906] GraphEngine: OpenCypher compilation error: Invalid subgraph: Unexpected argument type: line 1 col 57
  two BIGINTs compared (control)    : OK  [('a', 'b'), ('a', 'c'), ('b', 'c'), ('a', 'd'), ('b', 'd'), ('c', 'd')]
  BOOLEAN against a constant (G10)  : [4905] GraphEngine: OpenCypher syntax error: syntax error, unexpected token near "true": line 1 col 82 (at 
  BOOLEAN IS NULL                   : OK  [('c',)]
  SQL COUNT(b) over the table        : OK  [(3,)]

  => a BOOLEAN attribute is projected, grouped by, DISTINCTed and null-tested, but COUNT(bool) and
     a comparison of two BOOLEAN attributes are compilation errors (4906); SQL counts the column.
```

## How to judge
- Consistent with the documented "BOOLEAN is projected only" (probe G10: `a.B = $b` and
  `TOINTEGER(a.B)` are refused), so a quirk rather than a defect; `COUNT` of a projectable
  attribute being refused, with a message that names `boolean` as acceptable, is the odd
  corner. Refused cleanly (4906), nothing silently wrong.

## What the provider does
A native cypher text counts no BOOLEAN field (an `agg` count, a grouped count) and correlates
no self-join on one (`select._cypher_asks`); the views family asks those in SQL. Probe G33
records the behaviour; `ganda/hana/rules/catalog.py` (`accepts.count.boolean_attribute`, `accepts.compare.boolean_attributes`).
