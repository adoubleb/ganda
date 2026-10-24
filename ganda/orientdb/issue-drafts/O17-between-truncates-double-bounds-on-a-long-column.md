# O17 -- `BETWEEN` converts double bounds to a LONG column's type by truncation, where `>= AND <=` compares through double

**Version:** OrientDB 3.2.56. Found by the fuzzer's `edge_filter` family with cross-type pivots (seed 455 of campaign c60b): `f1 BETWEEN 716.5 AND 9007199254740992.0` over a declared LONG edge attribute missed three relationships whose `f1` is 9007199254740993 on the graph E class and the junction class alike (the reference compares through double, where 2^53 + 1 equals 2^53); the same shape is answered differently by the `>= AND <=` rewrite. Isolated live on a node class and on the doc side's embedded documents:

```sql
CREATE CLASS c1; CREATE PROPERTY c1.id STRING; CREATE PROPERTY c1.f1 LONG; CREATE PROPERTY c1.e1 EMBEDDEDLIST;
INSERT INTO c1 SET id = 'a', f1 = 0, e1 = [{w: 0}]; INSERT INTO c1 SET id = 'b', f1 = 1, e1 = [{w: 1}];
INSERT INTO c1 SET id = 'c', f1 = 5, e1 = [{w: 5}]; INSERT INTO c1 SET id = 'f', f1 = 9007199254740993;

SELECT id FROM c1 WHERE f1 BETWEEN 0.5 AND 5.5;               -- [a, b, c]   the bounds became 0 and 5
SELECT id FROM c1 WHERE f1 >= 0.5 AND f1 <= 5.5;              -- [b, c]
SELECT id FROM c1 WHERE f1 BETWEEN -3.5 AND 0.5;              -- [a]         (0 BETWEEN -3 AND 0)
SELECT id FROM c1 WHERE f1 BETWEEN 1 AND 9007199254740992.0;  -- [b, c]      the upper bound became the exact long 2^53
SELECT id FROM c1 WHERE f1 >= 1 AND f1 <= 9007199254740992.0; -- [b, c, f]
SELECT e1[w BETWEEN 0.5 AND 5.5].w FROM c1;                   -- [[0], [1], [5], []]   the embedded Integer too
```

`BETWEEN 1.0 AND 5.0` (integral bounds) agrees with the range form. Unlike O13 (an embedded Integer against a Long bound) this holds on declared LONG properties and on every side, with a scan or an index. The fuzzer withholds every side of a `BETWEEN` on an INT column (a node field or a relationship attribute) with a double bound that truncation changes (`guards.o17_predicate`); the `>= AND <=` rewrite of the same predicate stays admitted.


**Further faces (probed 3.2.56):** on an INTEGER column the bounds WRAP (`f BETWEEN 1 AND 2147483648` answers nothing over `{0, 5, 2147483647}`, and `f BETWEEN -2147483649 AND 2147483648` too), where `f >= 1 AND f <= 2147483648` answers `5, 2147483647`; on a FLOAT column a double bound is rounded to a float.
