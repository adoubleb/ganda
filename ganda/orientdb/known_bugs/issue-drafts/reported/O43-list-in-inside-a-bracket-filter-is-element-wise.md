# `f IN [[x]]` matches lists that are not equal to `[x]`

**OrientDB version:** 3.2.57 (official Docker image `orientdb:3.2.57`; also seen on 3.2.56)
**Java version:** OpenJDK 1.8.0_504
**OS:** Ubuntu 26.04 (container)
**Found by:** the cov400 campaign (seeds 5056, 5202): the select IR's `rw` twin spells a list equality `e[f = :p]` as `e[f IN [:p]]`, and on the doc side, whose relationship attributes live in undeclared embedded lists, the two answered different edges.

## Steps to reproduce

On an empty database:

```sql
CREATE CLASS d;
INSERT INTO d SET k = 'a', f = [0, 1];
INSERT INTO d SET k = 'b', f = [0];

SELECT k FROM d WHERE f IN [[0, 1]];         -- a      (right: a's list equals [0, 1])
SELECT k FROM d WHERE f IN [[1]];            -- a      (Wrong: expected no rows, neither list equals [1])
SELECT k FROM d WHERE f IN [[4294967296]];   -- a, b   (Wrong: expected no rows, neither list holds 4294967296)
```

## Expected behavior

| query | expected |
|---|---|
| `f IN [[0, 1]]` | `a` |
| `f IN [[1]]` | no rows |
| `f IN [[4294967296]]` | no rows |

## Actual behavior

| query | actual |
|---|---|
| `f IN [[0, 1]]` | `a` |
| `f IN [[1]]` | `a` |
| `f IN [[4294967296]]` | `a`, `b` |

The same happens inside a bracket filter (`SELECT e[f IN [[1]]] FROM ...`).

## Why this is a bug

`f IN [v1, v2, ...]` should hold when `f` equals one of the listed values. `[0, 1]` is not equal to `[1]`, yet it matches `[[1]]`, while `[[0, 1]]` is compared as a list. So the result depends on how many elements the listed list has. `[[4294967296]]` matches both rows although neither list contains that value.
