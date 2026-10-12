# A41: `optimize-traversals` evaluates a vertex filter it moved into the traversal on the start vertex, which the traversal never emits

**Version.** ArangoDB 3.12.11 (Docker `arangodb:3.12.11`, single server, default options).
Found by Ganda's ArangoDB port on 2026-10-09 in the first large-scale campaign (seed 3:
an `ir_traversal` case whose first hop, from class `n2` back to class `n1`, carried a
target predicate over `n1`'s map column `f5`; `n2` types its own `f5` as a number). The
`graph` data model's nested-traversal and `COLLECT`-folded variants were rejected while
the edge-scan, single-traversal, document and junction variants answered. Reduced on the
instance's data, then to the fixture below; re-checked by `ganda arangodb probe A41`.

## Steps to reproduce

```aql
// n1: a {m: 5}, b {m: {k1: 1}}, c {}      e1: a -> b, a -> c
FOR n IN n1 FILTER n.id == 'n1/a'
  RETURN {id: n.id, v: (
    FOR v1, ed1 IN 1..1 OUTBOUND n e1
      FILTER @p IN VALUES(v1.m || {})
      RETURN v1.id)}
```

with `@p = 1`.

## Expected

`[{id: 'n1/a', v: ['n1/b']}]`: the filter is evaluated on the emitted vertices `b`
(`VALUES({k1: 1})` holds 1) and `c` (`VALUES({})` is empty). That is what the query
answers with `OPTIONS {optimizer: {rules: ["-optimize-traversals"]}}`, what the same
filter answers over an edge scan (`FOR ed1 IN e1 FILTER ed1._from == n._id LET v1 =
DOCUMENT(ed1._to) FILTER ...`), and what the ternary `(IS_OBJECT(v1.m) ? @p IN
VALUES(v1.m) : false)` answers under the default optimizer.

## Actual

```
AQL: invalid argument type in call to function 'VALUES()' [node #4: TraversalNode] ... (while executing)
```

errorNum 1542 (a warning, turned into an error by `failOnWarning: true`; without it a
warning and a null). The only non-object `m` in reach is the START vertex's number
(`5 || {}` is 5). With a start lacking `m` (`n1/c` as the start) the query answers;
with a bind-variable start (`OUTBOUND 'n1/a' e1`) it fails the same way; so does a
`2..2` range, and `STARTS_WITH(v1.m || '', 'x')` on the same start.

## Analysis

`optimize-traversals` moves a filter on the traversal's vertex variable into the
traversal node as a vertex condition. The condition is evaluated for every vertex the
traversal visits, the start vertex at depth 0 included, although a `1..1` (or `2..2`)
traversal never emits the start and the query's own `FILTER` never sees it. A function
in the condition that rejects the start's attribute type raises, and the whole query
fails. The rule does not prune on a false start condition (the ternary form, false on
the start, still reaches `b`), so the face of the bug is the spurious error, not a lost
row. The sibling of the legacy port's **A32**
(`../arangoDB/issue-drafts/A32-optimize-traversals-reorders-a-moved-filters-conjuncts.md`):
the same rule, a different way the moved condition stops meaning what the filter meant.

## Scope

- Affected: any vertex filter moved into a traversal whose function rejects a type the
  start vertex may hold (`VALUES`, `ATTRIBUTES`, `STARTS_WITH`, ... on an attribute the
  start types differently from the emitted vertices -- natural across two vertex
  collections sharing an attribute name); a `||` or `NOT_NULL` default does not help,
  since the start's value is not null.
- Not affected: an edge filter (`ed1.m`); the same expression outside a traversal; a
  depth-range traversal filtering the path variable (`dp.vertices[1].m`); a filter
  whose function accepts every type (`LENGTH`).
- Workaround: a type test the function never sees through (`IS_OBJECT(v.m) ? ... :
  false`), or the rule switched off.

## How Ganda met it

Every data model renders one `TraversalQuery`; the error on two of the graph data
model's syntaxes against answers from seven other variants was an error asymmetry.
The provider now renders the map operators through the ternary (the question stays
in the supply), and the probe pins the behaviour so a fix shows as a changed fact.
