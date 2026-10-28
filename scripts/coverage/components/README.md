# Scope and components (written 2026-10-26, before the 8-hour runs)

One file per engine: `scope` is a regular expression over a source path (the path as the
line map reports it) that a file must match to count at all; `components` are ordered
`[regex, component]` rules, the first match deciding, `other` when none does. They were
written from the source trees' layout BEFORE any 8-hour result was seen and are not to
be tuned afterwards; a change after that is recorded here with its reason.

The components are the same ten for every engine, so the breakdowns line up:

| component | what it holds |
|---|---|
| parser | lexer, grammar, AST construction, bind parameters |
| planner | query planning and optimization (plan building, rules, index selection, cost) |
| executor | the execution engine: operators, iterators, the statement/document pipeline |
| expressions | expression evaluation, value arithmetic and comparison, built-in functions |
| graph | graph traversal and path algorithms outside the generic executor |
| indexes | index structures and their lookups |
| storage | the storage engine, keys and record serialization |
| transactions | transaction management |
| catalog | schema / metadata / catalog |
| server | network, protocol, request handling, authentication |

## Changes

- 2026-10-26, before any 8-hour run: checked against the 3-seed pilots' file lists
  (classification only); `lib/iresearch` (ArangoDB's search-index library) to indexes,
  `arangod/Cache` to storage, SurrealDB's `core/buc` (file buckets) to storage and
  `core/ctx` (query context) to executor. Utilities (`lib/Basics`, SurrealDB's `runtime`,
  `collections`) stay `other` on purpose.
