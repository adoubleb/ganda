# Depth-first TRAVERSE with MAXDEPTH misses records within the depth limit

**OrientDB version:** 3.2.56 (official Docker image `orientdb:3.2.56`)
**Java version:** OpenJDK 1.8.0_502
**OS:** Ubuntu 26.04 (container)

## Steps to reproduce

On an empty database (only the built-in `V` and `E` classes are used):

```sql
CREATE VERTEX SET name = 'r';
CREATE VERTEX SET name = 'a';
CREATE VERTEX SET name = 'b';
CREATE VERTEX SET name = 'c';
CREATE VERTEX SET name = 'x';
CREATE VERTEX SET name = 'z';
CREATE EDGE FROM (SELECT FROM V WHERE name = 'r') TO (SELECT FROM V WHERE name = 'c');
CREATE EDGE FROM (SELECT FROM V WHERE name = 'r') TO (SELECT FROM V WHERE name = 'a');
CREATE EDGE FROM (SELECT FROM V WHERE name = 'a') TO (SELECT FROM V WHERE name = 'b');
CREATE EDGE FROM (SELECT FROM V WHERE name = 'b') TO (SELECT FROM V WHERE name = 'x');
CREATE EDGE FROM (SELECT FROM V WHERE name = 'c') TO (SELECT FROM V WHERE name = 'x');
CREATE EDGE FROM (SELECT FROM V WHERE name = 'x') TO (SELECT FROM V WHERE name = 'z');

SELECT name, $depth FROM (TRAVERSE out() FROM (SELECT FROM V WHERE name = 'r') MAXDEPTH 3);
SELECT name, $depth FROM (TRAVERSE out() FROM (SELECT FROM V WHERE name = 'r') MAXDEPTH 3 STRATEGY BREADTH_FIRST);
```

The graph:

```
r -> c -> x -> z        (z is 3 hops from r)
r -> a -> b -> x        (a longer walk to x)
```

## Expected behavior

Every vertex is within 3 hops of `r` (`z` through `r -> c -> x -> z`), so both queries return all six vertices: `r`, `a`, `b`, `c`, `x`, `z`.

## Actual behavior

The default (depth-first) query returns five vertices. `z` is missing, and `x` is reported at `$depth` 3:

| name | $depth |
|------|--------|
| r    | 0      |
| c    | 1      |
| a    | 1      |
| b    | 2      |
| x    | 3      |

`STRATEGY DEPTH_FIRST` gives the same result. `STRATEGY BREADTH_FIRST` returns all six vertices, with `x` at depth 2 and `z` at depth 3.

The answer depends on the order the edges were created in. If the first two `CREATE EDGE` statements are swapped (`r -> a` before `r -> c`), the depth-first query also returns all six vertices.

## Analysis

The depth-first traverser marks a record as visited when it is pushed, with the depth of that first discovery, and it does not expand a record found at the MAXDEPTH limit. Starting from `r`, it pushes `c` and `a` (both at depth 1) and pops the last one pushed, `a`. Following `a -> b -> x` it reaches `x` at depth 3, which is the limit, so `x`'s edges are never followed. When it then pops `c` and follows `c -> x`, `x` is already visited and is skipped. So `z`, which is reachable only through `x`, is never visited, even though `x` is only 2 hops from the root.

As a result, `MAXDEPTH k` limits the length of the walk that happened to find each record first, not the record's distance from the root, and the result set changes with edge creation order. Two ways to fix it: revisit a record when it is reached at a smaller depth than the one it was recorded with, or document that depth-first `MAXDEPTH` does not return every record within `k` hops.

This is the smallest example we found. Under a model of the depth-first traverser that matched the engine on 40 random graphs, no graph with fewer than six edges or six vertices shows the problem.

## Scope

- **Affected:** `TRAVERSE ... MAXDEPTH k` with the default or `DEPTH_FIRST` strategy, whenever a record within `k` hops is first reached at the limit by a longer walk and has outgoing links. The result set and `$depth` are both affected, so `min($depth)` is not the distance either.
- **Not affected:** `STRATEGY BREADTH_FIRST`, and `TRAVERSE` without a depth limit.

## Report Status
Reported as 10894. Confirmed.