# O36 -- `f BETWEEN a AND b AND g op c` served by a composite index on `(f, g)` answers a lexicographic key range: the condition on `g` is not applied

**Version:** OrientDB 3.2.56. Found by the fuzzer's `node_filter_ids` family on a default-config campaign (tag `sample-default`, seed 3) as a within-database inconsistency: in the one database holding a composite index `(f1, f3)` on the class, `f1 BETWEEN :a AND :b AND f3 >= :c` returned five records, while the same database answered the reversed-operand spelling (`:c <= f3`) and the rewritten one (`NOT (NOT (...) OR NOT (...))`) with the correct two. The two other databases (no such index) and the reference agreed on the two records.

```sql
CREATE CLASS t; CREATE PROPERTY t.f LONG; CREATE PROPERTY t.g LONG;
INSERT INTO t SET f = 1, g = 10;
INSERT INTO t SET f = 2, g = 30;
INSERT INTO t SET f = 3, g = 10;

SELECT f, g FROM t WHERE f BETWEEN 1 AND 3 AND g >= 20;        -- {2, 30}             (scan: right)
CREATE INDEX t_fg ON t (f, g) NOTUNIQUE;
SELECT f, g FROM t WHERE f BETWEEN 1 AND 3 AND g >= 20;        -- {2, 30}, {3, 10}    (expected {2, 30})
SELECT f, g FROM t WHERE f >= 1 AND f <= 3 AND g >= 20;        -- {2, 30}             (the same range spelled with >= / <=: right)
```

The plans show the difference. The `BETWEEN` form hands the whole condition to the index and never filters afterwards:

```
+ FETCH FROM INDEX t_fg
  f BETWEEN 1 AND 3 AND g >= 20
+ EXTRACT VALUE FROM INDEX ENTRY
+ DISTINCT
+ FILTER ITEMS BY CLASS t
```

The `>= / <=` form uses the index for `f` only and filters `g` itself:

```
+ FETCH FROM INDEX t_fg
  f >= 1 and f <= 3
+ EXTRACT VALUE FROM INDEX ENTRY
+ FILTER ITEMS WHERE g >= 20
+ FILTER ITEMS BY CLASS t
```

The answer is the key range `[(a, c), (b, +inf)]` compared as a tuple (lexicographically), which equals the conjunction only when the lead is fixed. `(3, 10)` lies between `(1, 20)` and `(3, +inf)`, so it is returned; `(1, 10)` sorts below `(1, 20)`, so it is not. Each face below follows from the same rule (rows `(1,10) (2,30) (3,10) (4,30) (2,10) (null,30) (2,null)`, the scan's answer first):

| predicate | scan | composite index (`ignoreNullValues: true`) |
|---|---|---|
| `f BETWEEN 1 AND 3 AND g >= 20` | (2,30) | (2,30) (3,10) (2,10) |
| `f BETWEEN 1 AND 3 AND g > 20` | (2,30) | (2,30) (3,10) (2,10) |
| `f BETWEEN 1 AND 3 AND g <= 20` | (1,10) (3,10) (2,10) | + (2,30) |
| `f BETWEEN 1 AND 3 AND g < 20` | (1,10) (3,10) (2,10) | + (2,30) |
| `f BETWEEN 1 AND 3 AND g = 30` | (2,30) | (2,30) (3,10) (2,10) |
| `f BETWEEN 1 AND 3 AND g BETWEEN 20 AND 40` | (2,30) | (2,30) (3,10) (2,10) |
| `g >= 20 AND f BETWEEN 1 AND 3` (operands swapped) | (2,30) | (2,30) (3,10) (2,10) |

- **Affected:** a `BETWEEN` on the lead field combined with any comparison on the second field (`=`, `<`, `<=`, `>`, `>=`, `BETWEEN`), whatever the conjunct order; `NOTUNIQUE` and `UNIQUE` SB-tree indexes; LONG and DOUBLE columns. With `ignoreNullValues: false` the answer also includes the record whose `g` is null (`(2, null)` is in the key range too).
- **Not affected:** the same range spelled `f >= a AND f <= b`; one-sided ranges on the lead (`f > 1 AND g >= 20`, `f >= 2 AND g >= 20`); an equality or `IN` on the lead (`f = 2 AND g >= 20`, `f IN [1, 2, 3] AND g >= 20`); a hash composite index (it does not serve the range); a single-field index on `f`. (`f < 3 AND g >= 20` with `ignoreNullValues: false` returns the null-`f` record: that is O11.)

The query returns wrong rows with no error, and only on the database that holds the index. The plan pushes the whole conjunction into `FETCH FROM INDEX` and drops the residual filter. The planner seems to treat `BETWEEN` on the lead as if it fixed the lead's value, which is valid only for `=` / `IN`.

**Fuzzer handling:** withheld at its shape per side by `guards.o36_predicate`: a plain `BETWEEN` leaf on the lead of a range-capable composite index of the side, as a top-level conjunct, next to a plain top-level conjunct on a later field of that index, in either order. It sits in `node_case_known`, which covers the node families, the select IR, `tx_read` and the write IR's filter. The traversal IR uses `o36_shape`, which checks the source and target predicates. The reversed and rewritten twins are not served, so they still assert. The bundle it came from is `campaign_reports/sample-default-cases/bugs/sample-default-3-node_filter_ids-node_filter_ids_op_AND_BETWEEN_type_float_cross_n/`, and the fact is in `scripts/probe_osql.py`'s composite-index section.
