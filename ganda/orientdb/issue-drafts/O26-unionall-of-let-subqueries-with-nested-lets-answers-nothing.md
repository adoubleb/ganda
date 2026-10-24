# O26 -- `SELECT expand($c) LET $a = (...), $b = (...), $c = unionall($a, $b)` answers nothing when both parts carry a nested correlated LET; the inline `expand(unionall((...), (...)))` of the same parts is right -- until it is wrapped in a `SELECT ... FROM (...) WHERE id IN (SELECT ...)`, which loses the nested values

**Version:** OrientDB 3.2.56. Found by campaign smoke3 (seeds 502 and 508): the fuzzer's relational and edge-class spellings of a `both()` question with a field projection (`SELECT src_id AS id, $f0[0].v AS v FROM e2 LET $f0 = (SELECT f1 AS v FROM n2 WHERE id = $parent.$current.dst_id)` unioned with its mirror) returned an empty result while every other spelling answered the rows.

```sql
CREATE CLASS n2; CREATE PROPERTY n2.id STRING; CREATE PROPERTY n2.f1 STRING;
CREATE CLASS e2; CREATE PROPERTY e2.src_id STRING; CREATE PROPERTY e2.dst_id STRING;
INSERT INTO n2 SET id = 'a', f1 = 'x'; INSERT INTO n2 SET id = 'b', f1 = 'y'; INSERT INTO n2 SET id = 'c';
INSERT INTO e2 SET src_id = 'a', dst_id = 'b'; INSERT INTO e2 SET src_id = 'a', dst_id = 'a'; INSERT INTO e2 SET src_id = 'b', dst_id = 'c';

-- one part alone, through a LET: right
SELECT expand($a) LET $a = (SELECT src_id AS id, $f0[0].v AS v FROM e2 LET $f0 = (SELECT f1 AS v FROM n2 WHERE id = $parent.$current.dst_id));
--   [{a, y}, {a, x}, {b, null}]

-- two parts through LETs and unionall: NOTHING
SELECT expand($c) LET $a = (SELECT src_id AS id, $f0[0].v AS v FROM e2 LET $f0 = (SELECT f1 AS v FROM n2 WHERE id = $parent.$current.dst_id)),
                      $b = (SELECT dst_id AS id, $f0[0].v AS v FROM e2 LET $f0 = (SELECT f1 AS v FROM n2 WHERE id = $parent.$current.src_id)),
                      $c = unionall($a, $b);
--   []          expected 6 rows

-- the same parts inline: right
SELECT expand(unionall((SELECT src_id AS id, $f0[0].v AS v FROM e2 LET $f0 = (SELECT f1 AS v FROM n2 WHERE id = $parent.$current.dst_id)),
                       (SELECT dst_id AS id, $f0[0].v AS v FROM e2 LET $f0 = (SELECT f1 AS v FROM n2 WHERE id = $parent.$current.src_id))));
--   [{a, y}, {a, x}, {b, null}, {b, x}, {a, x}, {c, y}]

-- the same LET form without the nested LET: right
SELECT expand($c) LET $a = (SELECT src_id AS id, dst_id AS v FROM e2), $b = (SELECT dst_id AS id, src_id AS v FROM e2), $c = unionall($a, $b);
--   6 rows
-- the inline union under an outer filter that is itself a subquery: the nested values are lost, and a row with them
SELECT id, v FROM (SELECT expand(unionall((SELECT src_id AS id, $f0[0].v AS v FROM e2 LET $f0 = (SELECT f1 AS v FROM n2 WHERE id = $parent.$current.dst_id)),
                                          (SELECT dst_id AS id, $f0[0].v AS v FROM e2 LET $f0 = (SELECT f1 AS v FROM n2 WHERE id = $parent.$current.src_id)))))
WHERE id IN (SELECT id FROM n2 WHERE f1 = 'x');
--   [{a, null}, {a, x}]     expected [{a, y}, {a, x}, {a, x}]
-- ... WHERE id IN ['a'];    [{a, y}, {a, x}, {a, x}]   (a literal list: right)
-- ... WHERE id = 'a';       [{a, y}, {a, x}, {a, x}]   (right)
```

With one nested-LET part next to a plain one the LET form answers; with two it answers nothing, and the inline form is right until an enclosing `IN (subquery)` filter re-evaluates it with the correlation broken. The fuzzer spells every two-part union inline (`spellings._graph_edgescan`, `_rel_flat`) and withholds the hoisted `rel:flat` of a `both` question with a field projection (`guards.known_findings`, "O26").

**Second face (campaign c100c, seed 808):** the parts need not nest a LET -- an `IN (subquery)` inside them is enough. Over the same data, `SELECT id, v FROM (SELECT expand(unionall((SELECT src_id AS id, dst_id AS v FROM e4 WHERE dst_id IN (SELECT id FROM n2 WHERE f1 > 1)), (SELECT dst_id AS id, src_id AS v FROM e4 WHERE src_id IN (SELECT id FROM n2 WHERE f1 > 1))))) WHERE id IN (SELECT id FROM n2 WHERE f1 < 3)` answers one row of the three; the same union without the inner subqueries, a single part with one, or a literal outer filter are all right. The fuzzer withholds the hoisted `rel:flat` of a `both` question whenever its parts carry a subquery (a field projection or a target predicate).
