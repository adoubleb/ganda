# A9 — two OPENCYPHER_TABLE calls in one statement: the second call's parameters are typed in the wrong order

**Bucket:** A (candidate engine defect: a statement that is valid, and runs, call by call,
fails once two calls share it -- the parameter metadata of the second call is wrong).
**Version:** HANA Cloud 4.00.000.00.1788859455. **Repro:** `python3 repro.py`
**Found by:** the Ganda SAP HANA fuzzer, the 12-hour campaign of 2026-10-12 (bastion run
`camp12h`): seven error asymmetries on the graph data model's traversal texts -- a `starts`
call LEFT JOINed to a `paths` call -- whenever the paths call bound a string next to a
number (`[-10427] Conversion of parameter/column (3) from data type UCS2 (LE) to BIGINT
failed`, a client-side conversion to the type the server declared for that `?`).

## Claim
```
paths  = MATCH (t1:rv)-[j1:re1]->(s:rv) WHERE (s.RV_S >= $p0a AND s.RV_S <= $p0b) AND (j1.RE1_W = $p1)
         RETURN s.ID AS ID, MIN(t1.RV_I) AS V          -- RV_S NVARCHAR, RE1_W BIGINT
starts = MATCH (s:rv) WHERE (s.RV_S >= $p0a AND s.RV_S <= $p0b) RETURN s.ID AS ID

SELECT * FROM OPENCYPHER_TABLE(GRAPH WORKSPACE rw QUERY '<paths>' PARAMETERS ('p0a' = ?, 'p0b' = ?, 'p1' = ?))
    with ('o', 'z', -23)                                                       -> OK [('b', 1)]
SELECT s.ID, p.V FROM OPENCYPHER_TABLE(... '<starts>' PARAMETERS ('p0a' = ?, 'p0b' = ?)) s
  LEFT JOIN OPENCYPHER_TABLE(... '<paths>' PARAMETERS ('p0a' = ?, 'p0b' = ?, 'p1' = ?)) p ON p.ID = s.ID
    with ('o', 'z', 'o', 'z', -23)                   -> [-10427] Conversion of parameter/column (3) ... to BIGINT failed
```
Parameter 3 is the second call's `p0a`, a string compared with an NVARCHAR attribute; the
server types it BIGINT -- the type of `p1`. With `p1` declared first in the second call the
server converts the string itself and fails (`[339] invalid number: not a valid number string
'o'`). With the string pivots as constants, or with every parameter of the second call a
string, the two-call statement answers.

## Actual (`repro.py`, 2026-10-12)
```
  the paths call alone (p0a, p0b strings; p1 an integer)    : OK [('b', 1)]
  the starts call alone                                     : OK [('a',), ('b',)]
  starts LEFT JOIN paths (the fuzzer's traversal text)      : [-10427] Conversion of parameter/column (3) from data type UCS2 (LE) to BIGINT failed (invalid number: not a valid number string 'o')
  the same, p1 declared first in the second call            : [339] invalid number: not a valid number string 'o' (at pos 62)
  the same with the string pivots as constants              : OK [('a', None), ('b', 1)]
  two calls, both calls' parameters strings only            : OK [('a', 1), ('b', 2)]
```
The campaign met the same failure with a DOUBLE next to a string (`... to DOUBLE failed`),
and -- silently, where the mis-typing converts rather than fails -- three traversal
mismatches whose paths call mixed BIGINT and DOUBLE parameters (a double read as a BIGINT
truncates): `camp12h-2 ... distinct_count`, `camp12h-11 ... nexists`, `camp12h-17 ... emax`.

## How to judge
- Each call alone infers every parameter's type from the attribute it is compared with
  (probe G2: an int and a string in one call). The second call of a statement gets its
  types assigned in another order than its PARAMETERS clause declares them. The texts are
  the documented form (Property Graph Engine Reference, QRC 2/2026, `OPENCYPHER_TABLE ...
  PARAMETERS`); `CAST(? AS ...)` in a PARAMETERS clause is refused (`[7] feature not
  supported`, probe G2b), so a caller cannot type the parameters by hand.
- Impact: a statement with two parameterised calls fails, or -- worse -- converts a
  parameter to the wrong type and answers the wrong rows.

## What the provider does
One parameter type per call: a DOUBLE pivot is bound, every other pivot (an integer, a
string, a LIST's text) is a constant in the cypher text (`graph.cypher_filter`; probes G30 /
G34 say those constants are exact). Probe G32 records the behaviour. `RULES.md`, "The graph
data model".
