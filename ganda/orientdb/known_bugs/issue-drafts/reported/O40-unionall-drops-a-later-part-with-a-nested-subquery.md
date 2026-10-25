# O40 -- inside `expand(unionall(...))`, a part after the first whose `IN (subquery)` nests another subquery answers nothing

**Version:** OrientDB 3.2.56. Found while probing the path IR's relational spelling: the shortest-path hop count was computed as the union of junction "levels", where level *d* is a nested `IN (SELECT dst_id ...)` chain. It was then confirmed on fuzzer-drawn data: seeds 1090 and 1127, `rel:asc` answered `[]` where every other spelling answered `[{"v": 4}]`.

```sql
CREATE CLASS n1; CREATE PROPERTY n1.id STRING;
CREATE CLASS e1; CREATE PROPERTY e1.src_id STRING; CREATE PROPERTY e1.dst_id STRING;
INSERT INTO n1 SET id = 'a';
INSERT INTO e1 SET src_id = 'a', dst_id = 'b';
INSERT INTO e1 SET src_id = 'b', dst_id = 'c';
INSERT INTO e1 SET src_id = 'c', dst_id = 'd';
INSERT INTO e1 SET src_id = 'b', dst_id = 'b';

-- the second part alone: c, d, b
SELECT dst_id AS d FROM e1 WHERE src_id IN (SELECT dst_id FROM e1 WHERE src_id IN (SELECT dst_id FROM e1 WHERE src_id = 'a'));

-- as the SECOND part of a union: only the first part's row ('a')
SELECT d FROM (SELECT expand(unionall(
  (SELECT id AS d FROM n1 WHERE id = 'a'),
  (SELECT dst_id AS d FROM e1 WHERE src_id IN (SELECT dst_id FROM e1 WHERE src_id IN (SELECT dst_id FROM e1 WHERE src_id = 'a')))
)));
```

| union | answer |
|---|---|
| `unionall(X, L3)`: `L3` is the part above, `X` any part | only `X`'s rows |
| `unionall(L3, X)` | every row |
| `unionall(L3, L3)` | one copy of `L3`'s rows |
| `unionall(X, (... WHERE id IN (SELECT dst_id FROM (SELECT FROM e1 WHERE ...))))`, a FROM subquery inside the IN subquery | `X`'s rows only |
| `unionall(X, (... WHERE id IN (SELECT ... IN (SELECT ... IN (SELECT ...)))))`, three levels | partial: one of the three rows |
| `unionall(X, L2)`: `L2`'s `IN (subquery)` holds no subquery of its own | every row |
| `unionall(X, (SELECT ... FROM (SELECT FROM (SELECT FROM n1)) ...))`, nested FROM subqueries only | every row |
| `LET $a = X, $b = L3, $c = unionall($a, $b)` then `expand($c)` | every row |

- **Affected:** a union part in any position after the first whose WHERE holds an `IN (subquery)` that itself contains a subquery (in a nested `IN` or as its FROM target). It fails with or without indexes on the columns, with literals or bind parameters.
- **Not affected:** the same part in the first position, a part whose `IN (subquery)` is flat, a part that only nests FROM subqueries, and the LET-bound union.
- **Related:** O26's second face (c100c seed 808: an inline union whose parts hold an `IN (subquery)` loses values under the hoist's filter) may be the same defect.

**Fuzzer handling:** the path IR (`queries/path_ir.py`) spells the relational hop count three ways: LET-bound (`rel`), inline with the deepest level first (`rel:inline`), and inline in ascending order (`rel:asc`). `rel:asc` is withheld (`path_ir.o40_asc`) where the distance is 3 and within the bound. Level 3 is the first part that nests two subqueries, and it decides the minimum only there. Fact: `scripts/probe_osql.py`, "the IR's parity with the node families".

## Report Status
Reported as 10898. Confirmed.