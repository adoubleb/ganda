# ArangoDB: the rules that keep the oracle sound

Every fact below carries its status: **documented** (the ArangoDB manual), **legacy**
(probed by the legacy port `../arangoDB` on 3.12.4 / 3.12.11), or **probed** (re-checked
by `ganda arangodb probe` on the image a campaign runs; the probe's fact id in brackets).
A fact that fails changes the provider, never the probe.

## How a record is addressed

- Every document carries the model's logical id in `id` (`n1/r0`); its engine key
  `_key` is that id's key percent-encoded into the key alphabet (`dialect.document_key`:
  `a b` -> `a%20b`, `%` -> `%25`), so the engine id is a function of the logical id and
  the inverse needs no lookup. A key may hold `%`, `;`, `+`, `@`: in a document-API
  path it is percent-encoded once more (**legacy**: `%15` read as a byte; **probed**
  W3). The documented key alphabet is `[A-Za-z0-9_\-:.@()+,=;$!*'%]`, up to 254 bytes
  (**documented**, **legacy**).
- The `id` attribute carries a persistent index on every node collection, unique unless
  the id index axis drew a plain one (`dialect.ID_INDEX_KINDS`); never none, because a
  hop of the relational data model is a lookup by `id`.
- The doc data model's link object holds `target` = the engine id (so `DOCUMENT()` and
  a join on `_id` read it); the junction holds the LOGICAL ids `src_id` / `dst_id`.

## Value semantics (`oracle/semantics.py`)

- The order across types is TOTAL: null < bool < number < string < array < object, so
  `null < 5`, `null <= 5`, `null != 5`, `null == null` hold and `null > 5`, `null == 5`
  do not; an absent attribute reads as null and `HAS()` tells them apart (**documented**,
  **probed** V1, V8). The two-valued NOT therefore needs no null conjunct in a rewrite.
- Numbers are one type: `1 == 1.0`, `-0.0 == 0`; a 64-bit integer against a double is
  compared EXACTLY (`2^53 + 1 == 9007199254740992.0` is false; **probed** V2 over the
  numeric boundary pairs). An integral double comes back as an integer on the wire, so
  every variant's rows are canonicalized the same way (`Semantics.canonical`). No signed zero is ever stored
  (`physical_design.BOUNDARY_VALUES`): the document API keeps the sign and every AQL
  write path drops it (A2), and a stored `-0.0` is one value to comparison and two to
  hashing (A1) -- a divergence of the write door drawn, not of the storage model.
- Strings are equal code point by code point (no Unicode normalization: an NFD `e` +
  U+0301 is two code points, not `==` U+00E9); their ORDER is ICU collation, upper
  case first (`'B' < 'a'`), which Python's order does not reproduce (**legacy**,
  **probed** V3). So a STRING (or BINARY) column takes the equality family only in the
  generated filters (`pivots.ordered_ops`). The model's DATETIME / DATE values are strings of
  one fixed shape, on which ICU and code-point order agree digit by digit.
- `IN` compares with `==`, so a null element matches a null value (**probed** V4): a
  semi-join's answered nulls match the records lacking the field.
- `LIKE` is case-sensitive; `%` and `_` cross `\n` and `\r` but not NEL (U+0085) nor
  the Unicode line and paragraph separators (U+2028, U+2029); the whole value must
  match; a null is read as the empty string (**probed** V5 over a value x pattern
  pool against `semantics.like_hit`). No pattern is built from a value holding a
  wildcard, the escape or a terminator (`pivots.LIKE_WILDCARDS`).
- `SUM([])` and `COUNT_DISTINCT([])` are 0; `MAX`, `MIN`, `AVERAGE`, `MEDIAN` of
  nothing are null; a keyless `COLLECT AGGREGATE` over no rows answers ONE row (`COUNT`
  0, `MAX` null, `SUM` null -- so `SUM` has no collect form) (**probed** V6). Sums run
  in double: exact only over integers whose absolute total stays under 2^53.
- `%` takes the dividend's sign; `7 / 2` is 3.5; an integral double comes back as an
  integer on the wire (**probed** V7); the oracle compares numbers by value.
- `SORT` puts null first ascending and last descending (**probed** V9).
- `MAX` / `MIN` over a string column order by collation.
- `HAS()` and `VALUES()` reject a non-object (errorNum 1542 under `failOnWarning`): the
  map operators answer false for a non-object through a ternary, `IS_OBJECT(n.m) ?
  ... : false` (a ternary evaluates the taken branch only), never through `n.m || {}`:
  a vertex filter the `optimize-traversals` rule moves into a traversal is evaluated on
  the START vertex too (depth 0, which the loop never emits), so a start whose attribute
  holds a value of another type (a number under `m || {}` is the number) fails the
  whole query with errorNum 1542 where the plain plan answers (**A41**, found live at
  the large scale on a hop between two classes typing the same field name differently;
  **probed** A41).

## Statement forms (`concrete_syntax/`)

- A depth-1 traversal, an edge scan (`e._from == n._id`), the link array
  (`DOCUMENT(l.target)` or a join on `_id`), the junction join (`j.src_id == n.id`, then
  `v.id == j.dst_id`) and the inline array operator answer one row per edge: a
  self-loop once per direction, a parallel pair twice (**probed** S1). The `both`
  direction (`ANY`) walks a self-loop twice, as the `UNION` of the two legs does
  (**probed** S2); `UNION` keeps duplicates.
- A `k..k` traversal under `uniqueEdges: 'none'` enumerates every walk of k edges (the
  nested depth-1 chain); a `0..k` traversal under `uniqueVertices: 'global', order:
  'bfs'` is the complete reach set (**probed** S3).
- `COLLECT ... INTO g = <expr>` keeps every row of a group; `AGGREGATE MAX / MIN` skip
  null; a start record with no path is absent from a folded result (**probed** S4).
- `NOOPT(n.f)` is never index-served; `OPTIONS {disableIndex: true}` and `OPTIONS
  {indexHint: ..., forceIndexHint: true}` are taken -- a forced hint the index cannot
  serve is HTTP 500 errorNum 1577 (a composite index under a condition on its lead
  field alone, a sparse index under `<` / `<=`, a function such as `IN_RANGE`), which
  the connection layer reads as a rejection, and the hint door names a single-field
  index for an equality, a membership or a bound only (**found live**); a subquery
  target is inlined;
  `COLLECT WITH COUNT INTO` and `COLLECT ... INTO g` agree with the function forms
  (**legacy**, **probed** S5).
- Every variable of a query is fresh, in nested scopes too: a semi-join's subquery
  variables are named after the leaf's bind (`s_p_0`), a hop's after its depth
  (`v1`, `ed1`, never `e1`: an edge collection is named that), a frontier's after its
  level and leg (**documented**, **probed** S6, S7).
- A bind variable the text does not name is errorNum 1552 (**documented**): the
  session passes each text the names it uses (`connection.bind_subset`), so one case's
  bind map serves every variant.
- The path forms (`SHORTEST_PATH`, the first of `K_SHORTEST_PATHS` and
  `ALL_SHORTEST_PATHS`, a bfs traversal, `MIN` over the depth-first walk, the frontier
  levels of the doc and rel data models) agree on the vertex count, answer one vertex
  for the start itself and nothing for an unreachable or too-distant goal (**probed** S7).

## Write doors (`storage/mapping/`, `concrete_syntax/write.py`)

- A data-modification subquery counts its rows (`LET r = (... RETURN 1) RETURN
  LENGTH(r)`); `RETURN OLD` / `NEW` after the modification; `OPTIONS {keepNull: false}`
  unsets; `REPLACE n WITH MERGE(n, {...})` keeps every other attribute, the link arrays
  included; `UPSERT ... RETURN 1` answers one row (**probed** W1).
- `UPDATE` MERGES a map value key by key into the stored one by default: every update
  door carries `mergeObjects: false`, or a map that lost a key would keep it (**found
  live**, seed 2 of the first smoke: a state diff on all three data models).
- A stream transaction names the collections the DATABASE has: the doc data model has
  no edge collections (**found live**, errorNum 1203 on the door's `begin`).
- A stream transaction's writes are visible inside it and gone after an abort; every
  setup door round-trips: the document API's bulk insert and `FOR d IN @docs INSERT`,
  `UPDATE @key WITH` (with `keepNull: false` for the dropped fields, separately, or an
  explicit final null would be stripped too), `REPLACE`, `UPSERT`, `INSERT ...
  overwriteMode: 'replace'`, `PATCH` and `PUT` (**probed** W2).
- Removing a vertex removes NO edge: the provider cascades explicitly on every data
  model (the edge documents, the junction rows, the arrays pointing at the victim) and
  a dangling link exists only as a mutant (**legacy**, **probed** W3).
- A bulk insert answers 202 with per-document errors inside the array; the connection
  layer raises the first (**legacy**).

## Limits (`engine/limits.py`)

- A request body of half the declared bound (`HTTP_MAX_LENGTH`) is accepted; 2000
  documents travel in one bulk insert (**probed** L1). A document of the record bound
  with a multi-byte string, a base64 text and a long array round-trips (**probed** L2).
- A unique persistent index takes a key of `max_index_key_bytes` and far beyond it
  (RocksDB keys have no documented bound) (**probed** L3). A hub of
  `max_edges_per_node` links and edges round-trips and traverses (**probed** L4).
- One stream transaction's writes are bounded by `--transaction.streaming-max-
  transaction-size` (**documented** default 128 MB): the server is started with
  `STREAMING_TRANSACTION_MAX_BYTES`. A stream transaction SLOWS with its write batch:
  a large setup of a hundred thousand single-statement requests in one transaction
  ran for over an hour on one core (**found live**, the first large campaign). Under
  the `statements_per_request` axis the churn is folded into bulk statements (`FOR r
  IN @rows UPDATE r.key WITH r.doc ...`, a bulk PATCH / PUT; `mapping.common.fold_steps`,
  each document named once per statement) and the transaction door commits one stream
  transaction per `chunk_size` documents (`transaction_groups`).
- The doc data model's source collections carry an array index over the link targets
  (`fk_<e>_target` on `e[*].target`): the reverse lookup a document schema keeps on an
  embedded reference, which serves the `join` syntax's incoming hop (`n._id IN
  (s.e || [])[*].target`) where the plain enumeration scans every source per start
  record (**legacy**: the planner takes the expanded index for `x IN doc.e[*].target`).

## Timeouts

- A query past the cursor's `maxRuntime` is killed with errorNum 1500, which the
  session reads as a timeout (`EngineError.timeout`): no answer, left out of the
  comparison at the large scale (**probed** T1). A cursor gone before its next batch
  (errorNum 1600 on the continuation: a self-join over thousands of records answers
  millions of ids, and the default time to live is 30 s between fetches) is no answer
  either; the session asks for an hour of `ttl` and 5000 rows per batch (**found live**,
  the first large campaign).

## Known findings of the legacy port and this surface

The legacy port's A1..A39 (`../arangoDB/issue-drafts/`) are either outside this surface
(ArangoSearch views, MDI and geo indexes, the JavaScript door, `-@x`) or kept
unreachable by construction: no signed zero (A1, A2), no ordered comparison on a string
column (A9, A14, A34), no `JoinNode` shape on a top-level two-loop (A20: the `rel`
join sits inside the per-record subquery).

- **A40** (`issues/A40-not-in-over-a-non-array-disagrees-with-not-of-in.md`, this
  port's first smoke): `NOT (x IN n.list)` is false over a null, absent or scalar
  attribute (the optimizer's `NOT IN` rewrite) while the constant form is true, so a
  filter and its De Morgan rewrite disagree. Guarded in `known_bugs/guards.py` at the shape
  (a list membership under a NOT over a column holding a null), pinned by the probe
  (**probed** A40).

- **A41** (`issues/A41-optimize-traversals-evaluates-a-moved-filter-on-the-start-vertex.md`,
  the first large campaign, seed 3): a vertex filter moved into a traversal by
  `optimize-traversals` is evaluated on the start vertex as well, so `@p IN
  VALUES(v.m || {})` fails with errorNum 1542 when the START holds a number under `m`
  (its neighbours all hold maps), where the edge scan, the rule switched off and the
  `IS_OBJECT` ternary answer. The provider renders the map operators through the
  ternary, so no guard is needed; the probe pins the behaviour.

A new finding gets an id, a write-up under `issues/` and a guard in `known_bugs/guards.py`.
