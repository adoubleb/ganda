# `shortestPath()` with `maxDepth: k` does not find a path of k hops

**OrientDB version:** 3.2.57 (official Docker image `orientdb:3.2.57`; also seen on 3.2.56)
**Java version:** OpenJDK 1.8.0_504
**OS:** Ubuntu 26.04 (container)
**Found by:** probing the graph functions for the fuzzer's `graph_shortest_path` family.

## Steps to reproduce

On an empty database:

```sql
CREATE VERTEX SET name = 'a';
CREATE VERTEX SET name = 'b';
CREATE EDGE FROM (SELECT FROM V WHERE name = 'a') TO (SELECT FROM V WHERE name = 'b');   -- a -> b: one hop

-- the path's vertex count: 2 is the path [a, b], 0 is no path
SELECT shortestPath((SELECT FROM V WHERE name = 'a'), (SELECT FROM V WHERE name = 'b'), 'OUT').size() AS n;                          -- 2
SELECT shortestPath((SELECT FROM V WHERE name = 'a'), (SELECT FROM V WHERE name = 'b'), 'OUT', null, {'maxDepth': 1}).size() AS n;   -- 0   (Wrong: expected 2)

-- TRAVERSE with the same depth limit reaches b
SELECT name FROM (TRAVERSE out() FROM (SELECT FROM V WHERE name = 'a') MAXDEPTH 1);   -- a, b
```

## Expected behavior

Both `shortestPath` queries return `n = 2`: the one-hop path `[a, b]`. The `TRAVERSE` returns `a` and `b`.

## Actual behavior

The `shortestPath` query without `maxDepth` returns `n = 2`, but with `{'maxDepth': 1}` it returns `n = 0` (no path). The `TRAVERSE` returns `a` and `b`.

In general `{'maxDepth': k}` finds paths of at most k - 1 hops: on a chain `a -> b -> c`, the path from `a` to `c` is found with `maxDepth` 3 but not 2.

## Why this is a bug

A one-hop path is within a depth limit of 1, so `maxDepth: 1` should not exclude it. The other depth limits in the engine, TRAVERSE `MAXDEPTH` and MATCH `maxDepth`, include paths of exactly k hops.

## Report status
Reported as 10914