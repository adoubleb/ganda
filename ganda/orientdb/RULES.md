# OrientDB: the rules that keep the oracle sound

Every rule below was paid for by a campaign bundle or a live probe against OrientDB
3.2.56. They were carried over from the legacy OrientDB fuzzer (`../OrientDB/CLAUDE.md`,
which also holds the full history behind each one) with the names translated to
ganda's vocabulary. Module names are relative to `ganda/orientdb/` unless they start
with `ast.` / `generators.` (the core). Where a rule names a retired node family or the
LLM cards, it names a question's shape; those subsystems were not ported.

Before relying on a new engine fact, add it to `scripts/probe_osql.py`
(`ganda orientdb probe`); before adding a known-bug guard, make the probe show the bug.

- **Records are addressed by the logical `id` property, never by `@rid`.** RIDs differ
  per database. A setup step that needs one carries a `Ref(id)` the connection layer resolves
  from the RIDs it captured (`Sql.capture`); a script/batch answers with its last
  result only, so captures are re-read afterwards, and a node the same script deletes
  is legitimately absent.
- **Every property is declared.** OrientDB coerces by declared type on write (`5.5`
  into a LONG is `5`, a bool is null) and types a schemaless value by its JSON. Every
  generated value has the column's type; every pivot has the column's type (a LONG
  compared with a double literal goes through double: `2^53 + 1 = 9007199254740992.0`).
- **Values travel as bind parameters** (`:p`), shared by every data model. The exceptions are
  the two statements that refuse a parameter -- `UPDATE ... CONTENT` and `MERGE` take a
  JSON literal (`json.dumps`, never SQL-interpolated) -- and the `/batch` door, which
  takes POSITIONAL parameters and stringifies a map (`{x=1}`), so a step carrying a
  map or list runs outside the batch.
- **`LET` precedes `WHERE`; `$parent.$current` reaches ONE level up.** A nested hop's
  subquery refers to its immediate parent's junction row (`$parent.$current.dst_id`),
  never `$parent.$parent`. A one-record lookup is indexed `[0]` before its field is
  read, or an aggregate over the list-of-lists is a ClassCastException.
- **No comparison operator in a projection** (parse error): booleans are rendered as
  filters (`WHERE expr.size() > 0` returns the id set) or as counts.
- **Multi-hop link paths are nested lists** (`e1.target.e1.target.id`); the oracle
  flattens a multiset and drops nulls (an absent list projects null, `.size()` on it is
  0). A bracket filter beyond the first hop of such a path matches nothing (O9), so
  `doc:path`/`graph:bag` are withheld there; `count`/aggregates are admitted for
  one-hop shapes only (`syntaxes.applicable`).
- **Strings compare by UTF-16 code units, but ORDER BY collates** (O6). `orient_key`
  orders by `str.encode("utf-16-be")`, and `node_max_vs_order` withholds its STRING
  variant.
- **`-0.0 < 0.0`** (`Double.compare`): `d = 0` misses a stored `-0.0`, GROUP BY keeps
  the two apart, and `orient_key` does too.
- **Null**: `IS NULL` and `<>` hold for an absent or null field, nothing else does;
  `IS DEFINED` tells absent from explicit null. `max`/`min` ignore null; `sum` over
  nothing is 0; nulls sort last ASC and first DESC. A null PIVOT (a value join binds
  a record's own null field): `<>` holds for every row, `<=`/`>=` for the null rows
  only, nothing else; a null element of an `IN` list matches nothing. Boolean logic
  is two-valued: `NOT (f > 5)` holds for a null `f`.
- **Cross-type numbers compare as the wider Java box on a scan** (Integer < Long <
  Float < Double: `5 = 5.0`, `2^53 + 1 = 9007199254740992.0`, an INTEGER against a
  Long as longs, a FLOAT against an integral pivot as FLOATS; `semantics.predicate_hit`
  converts per column type) -- an index converts the key to the column's type
  (truncation on LONG, WRAPPING on INTEGER, rounding on FLOAT: O16) and `BETWEEN`
  converts its bounds (O17). A DECIMAL against a double goes through the double's
  binary expansion on a scan and through its decimal string in an index or a BETWEEN
  bound (O20); `predicate_hit` models the scan, the pivots are half dyadic. `sum()`
  over Integers is 32-bit (O19): an INTEGER column and an embedded Integer attribute
  are summed only where the total fits an int.
- **Every value of a narrow type is exact on the JSON wire**: int32 values; float32
  values that are multiples of 1/8 below 2^10 (or 2^24), whose shortest decimal is
  their value; decimals with at most three places (stored through
  `BigDecimal.valueOf`, read back as the same double); no `-0.0` in a DECIMAL.
- **`COLLATE ci`**: `=`, `<>`, `<` .. `>=` and a value join compare lower-cased on a
  scan and through an index (which stores the lower-cased key, so `_collides`
  lowers); `IN` / `BETWEEN` compare code units on a scan (O21); a bracket filter, a
  dotted link field and a PROJECTED value ignore the collation (O25: `BRACKET`
  concrete syntaxes withheld, ci source predicates not pulled-up); `GROUP BY` / `DISTINCT` /
  `max` / `min` do not merge case variants. `LIKE` is case-insensitive on every
  string; `?` is its wildcard, `_` a literal; neither `%` nor `?` matches a line
  terminator (O42: a regex without DOTALL; `semantics.like_hit` models it).
- **`SELECT DISTINCT` may answer a row twice** (O41): its dedup `HashSet` treeifies a
  full bin and `OResultInternal.compareTo` (1 for any two non-record rows) misses an
  equal row of equal hash. Structured hashes collide (`null` / `0` / `-1` hash 0;
  `2^31` / `2^63-1` / `Long.MIN` hash `MIN_VALUE`; `v` and `-v-1`); `ganda/orientdb/known_bugs/java_hash.py`
  models the JDK hashes and `known_bugs.o41_values` withholds a DISTINCT whose rows could
  fill such a bin. GROUP BY and `set()` are right.
- **Index metadata is always explicit.** The engine's default is `ignoreNullValues:
  false`: an ABSENT field is the key `null`, held once by a UNIQUE index, so the
  second record lacking the field is rejected. `IndexSpec.ignore_nulls` is rendered
  out both ways and `_collides` models exactly that.
- **`IS [NOT] DEFINED` does not survive a projection** (an absent field projects as
  an explicit null), so a source predicate holding one is not pulled-up. `expand()`
  over an embedded list wraps each document under `value` (no edge attribute through
  `let_expand`). A TRAVERSE rooted at `$parent.$current` is correlated (O1).
- **Two-part unions are rendered inline**: `expand(unionall((a), (b)))`. The
  LET-bound form (`LET $a = (a), $b = (b), $c = unionall($a, $b)`) answers nothing
  when both parts carry a nested correlated LET, and even the inline form loses
  values under an enclosing `IN (subquery)` filter (O26: the pulled-up `rel:flat` of a
  `both` question with a field projection is withheld). MATCH negative patterns are
  broken (O22), so `nexists` never uses them; `optional: true` answers a null row.
- **An ORDER BY without the id tiebreak may be an index walk**: a composite index
  walked for its fields (or a one-direction prefix) drops the null-keyed records with
  `ignoreNullValues: true` (O18) and puts them first ascending with `false` (O23);
  `node_ordered_keys` is withheld per data model at those shapes; `node_ordered_page`'s
  tiebreak always sorts; `node_max_vs_order`'s ASC `:order` data model is withheld where a
  composite index led by the field ignores nulls and a record with the field has a
  null in another component (no entry: the walk skips it, c100e seed 1049). A `<` / `<=` on the second field of a composite index next
  to an equality on its lead is index-served (O11's face), and so are O10's, O16's
  and O21's shapes there (`known_bugs._composite_served`).
- **`UPDATE ... INCREMENT` is refused by the new executor**; the arithmetic door is
  `SET f = f + :d`, whose delta must itself be a long (c100 seed 605). `UPDATE EDGE
  CONTENT` keeps the endpoints.
- **Dates are their `yyyy-MM-dd HH:mm:ss` strings** (UTC, a DATE at midnight):
  `predicate_hit` compares them lexicographically and normalizes a pivot the way the
  engine converts it (`semantics.canonical_date`: a date alone -> midnight, epoch
  millis -> the second). A partially matching string (ISO `T`, no seconds) is
  compared as the date's midnight (O27, withheld everywhere). Never `LIKE` a date.
- **A map entry is a leaf with `key`** (`m.k op :p`, or `m[:kb]` with `key_bind`),
  never index-served and never `IS DEFINED` (holds for every record, an O8 face);
  `CONTAINSKEY` is served by a `BY KEY` index, `CONTAINSVALUE` by `BY VALUE` (an
  Integer-boxed pivot misses Long values on a scan: O7's map face, withheld). Guards
  skip map-entry leaves for every index rule (`known_bugs._leaf_served`).
- **The `index:` door** cannot filter by record or class, so only the table's own
  single-field (and composite, `key = [a, b]`) indexes are read through it; a
  range is O3, a BETWEEN on a hash kind is O5 -- neither is emitted. Reach
  questions: the start is included by every form (TRAVERSE never revisits its
  root, so "without the start" is not uniform); the relational form nests a
  correlated LET per level with an UNWIND at each (an index-served `IN (subquery)`
  correlated to the grandparent is an O2 NPE); `expand()` does not flatten.
- **Inheritance**: `Schema.base` is an abstract class every node class extends;
  `schema.query_fields(table)` (own + base fields) is what every query-side lookup
  uses, `schema.node_indexes` includes the base's indexes for a subclass and every
  subclass's for the base, `instance.nodes_of(base)` is every node.
- **A sum is emitted only where every partial sum stays a long** (`sum_is_exact`,
  |total| < 2^62): long arithmetic wraps silently. `/` on longs yields a double when
  inexact (`3 / 2` is `1.5`, `2 / 2` is `1`).
- **MATCH takes no TIMEOUT clause**; the connection layer appends `TIMEOUT ... EXCEPTION` to
  SELECTs only (a script's inner SELECT carries its own). **A MATCH with a depth
  bound enumerates PATHS**: `maxDepth: n` on a dense graph never terminates (the
  fifth round's first smoke hung a worker for 120 s per seed); `graph_shortest_path`
  bounds its MATCH forms at 3 and lets only TRAVERSE (visited-set) go deep. `MAXDEPTH`
  and a MATCH `maxDepth:` take no bind parameter (interpolate the small integer);
  `while: ($depth < :k)` does.
- **Expression arithmetic and reversed operands are scans** (never index-served).
  Over an INTEGER `*` wraps in 32 bits and `+` / `-` wrap on one sign of overflow
  (O29); a null operand is the identity of `+` / `-` and absorbs `*` / `/` / `%`
  (O30): `predicates.arithmetic` emits a rewrite only where the arithmetic is exact
  on every stored value and the pivot (int32 / long, float32-exact, a double
  scaled by a power of two -- a double SUM rounds and collapses adjacent values,
  dyadic decimals, dates by milliseconds), and handles the null rows explicitly
  (`AND f IS NOT NULL`, `OR f IS NULL` for `<>`). **The comparison converts the
  RIGHT operand to the LEFT one's type** (O32): a reversed leaf with a string pivot
  on a DATETIME / DATE column compares the Date's Java `toString()` (withheld);
  numbers, strings, ci and the null rules are symmetric. The arith rewrite binds
  under `ar` / `arc`; a bind prefix shared with the rewrite's `q` silently
  overwrote the rewrite's pivot in the fifth round's first smoke.
- **The transaction door**: a script `BEGIN; <write>; LET $r = (<read>); ROLLBACK;
  RETURN $r` answers `[{"value": rows}]` (`connection.unwrap_script`; a case text that
  starts with `BEGIN;` runs through `/command/<db>/script`, `explain` is skipped).
  The graph data model deletes with `DELETE VERTEX` and inserts with `CREATE VERTEX`
  (`DELETE FROM` / `INSERT INTO` a vertex class are refused / fine respectively --
  the insert is fine, the delete is not); a value reused from another row is never
  written to a column a UNIQUE index of any data model covers (a duplicate key inside
  the transaction raises nothing before COMMIT and the unique lookup answers one of
  the two). Inside the transaction an index-served ONE-SIDED range answers the
  changed records inverted and misses the inserted ones (O31, `known_bugs.o31_known`:
  withheld after an update of the range's column or an insert, on a data model whose
  SB-tree index serves it), and a range with REVERSED bounds (`BETWEEN 6 AND 4`,
  `>= 6 AND <= 4`) through such an index is `fromKey > toKey` once the column was
  written (c100d seed 1046; the same guard); a single-key DESC `ORDER BY` walk of
  such an index answers the written (or inserted) record FIRST, whatever its value
  (cov400; `known_bugs.o31_desc_walk`: the `keys` kind and `max`'s `:order` form); a
  semi-join's inner query reads the class through its index under every position,
  the `:scan` rewrite's included (`_tx_known`); everything else -- equality, IN,
  well-ordered two-sided ranges, BETWEEN, ASC walks, an id-tiebroken order, the
  `index:` door, scans, deletes, graph writes -- is right. The runner re-verifies the live state after
  the cases (`state leaks`). A DDL statement is refused inside a transaction.
- **`shortestPath((SELECT ...), (SELECT ...), :dir, 'e' | null, :o)`** answers the
  path's vertex count, 0 when unreachable, 1 for the same record; `{maxDepth: k}`
  stops one hop short (O35: the `graph` data model is withheld where the BFS distance is
  exactly k); an aggregate over no rows answers no row, so every form of the query generator
  is wrapped to answer nothing when unreachable (`WHERE v > 0`, `min($depth) + 1`).
  **TRAVERSE's default strategy is DEPTH_FIRST and `$depth` is the depth of first
  discovery in that order** (c100e seeds 1009 / 1087): a `min($depth)` distance
  needs `STRATEGY BREADTH_FIRST`, and so does the reach SET: depth-first marks a
  record visited when it is pushed and does not expand one met at the bound, so a
  record reached first through a longer walk keeps its children unvisited (O44,
  cov400 seeds 5242 / 5342). The reach concrete syntaxes `traverse` / `traverse_e` render
  BREADTH_FIRST; `traverse_dfs` is withheld where the graph allows it
  (`known_bugs.o44_dfs_reach`).
- **`CONTAINSALL` / `CONTAINSANY` compare boxes**: an Integer-boxed element never
  equals a stored Long on a scan (O7's list face, `known_bugs.list_ops_known`); a list
  index serves `CONTAINSANY` by converting the keys and answers a ClassCastException
  for a mixed-box list (O10's face), a HASH or DICTIONARY list index a 501 (O5's
  face, c100d seed 1022); in a bracket filter, a MATCH where, a correlated subquery
  (the traversal concrete syntaxes), a base-class `:poly` query (c100d seed 1046), under a
  NOT or an OR (`known_bugs._conjunct_leaves`: only a top-level conjunct is served) or
  on `tx_read`'s `:scan` rewrite (both from the benchmark's healthy run, seed 3)
  nothing is served, so every data model is the scan there (`servable=False`). `count(distinct(f))` counts every non-null row
  (O33: `node_agg`'s `distinct_count` asks a DISTINCT subquery and `set(f).size()`
  instead). DECIMAL equality is scale-sensitive in DISTINCT / GROUP BY / `set()` /
  a scan's `IN (subquery)` / a string pivot (O34, filed by the probe: the fuzzer's
  decimals are written from doubles, one canonical scale).
- **Known findings are withheld at their exact shape, per SIDE** (`case.known_bugs`,
  `known_bugs/guards.py`): O1 (correlated subquery under an index-served outer, including
  `rel:flat`'s `IN (SELECT id ...)` on the always-indexed FK columns next to a LET),
  O2 (correlated equality + predicate on an indexed field: NPE. The whole rule, probed for the cards (`cards/known_bugs._o2_tree`): inside ONE subquery of the outermost query, a `$parent.$current` read anywhere -- a WHERE, a projection, a nested or expression LET -- plus a condition an index serves at ANY of its levels -- its own WHERE, a FROM target, a TRAVERSE root, an `IN (subquery)`, a sibling nested LET -- throws; a subquery inside a bracket filter throws with no correlation at all; `<>`, `IS NOT NULL`, reversed / arithmetic operands and a comparison with a variable are not served; the outermost query's FROM chain is O1 instead; the SCRIPT door answers every shape), O4 (DICTIONARY index
  serving equality), O6, O7 (list equality on a declared list), O8 (`link.f IS
  DEFINED`), O9, O10 (`IN` mixing Integer and Long through an index), O11 (`<` on the
  lead of a composite index returns null rows), O12 (a double literal parses as a
  float32; the `:literal` variant of `node_filter_ids`), O13 (`BETWEEN` with a Long
  bound over an embedded Integer, the doc data model's edge attributes), O14 (`IN (SELECT
  ...)` with a null in the answer through an index: `node_in_subquery` per indexed
  data model), O15 (doc-side `emax`/`emin` over an INT edge attribute whose stored values
  mix widths), O16 (a truncation-sensitive double pivot on an indexed INT column:
  `=`/`IN` on any kind, `<`/`<=`/`>`/`>=` on a range-capable kind), O17 (a `BETWEEN`
  with such a bound on an INT column, every data model), O18 (`node_max_vs_order`'s `:order`
  data model for `max` where the data model holds a range index with `ignoreNullValues: true`: a
  single-key DESC walk of such an index answers no rows; and `node_ordered_keys` on
  a composite-index walk), O19 (the doc data model's `esum` over Integer attributes whose
  total leaves int32), O20 (a non-dyadic double pivot on a DECIMAL column an index
  serves), O21 (`IN` / `BETWEEN` on a ci column an index serves, `node_in_subquery`
  over a ci column on an indexed data model), O23 (`node_ordered_keys` on a composite
  index that keeps nulls), O25 (a `BRACKET` concrete syntax with a ci comparison in a target
  predicate, or `edgescan`'s source predicate), O26 (the pulled-up `rel:flat` of a
  `both` question with a field projection), O27 (a partially matching date string
  pivot, every data model), O28 (`node_agg`'s `avg` over integers with a non-integral
  mean, every data model), O31 (`tx_read`: a one-sided range served by an SB-tree index
  after the transaction wrote the column or inserted), O32 (a reversed leaf with a
  string pivot on a date column, every data model), O33 (`count(distinct(f))`, every
  data model), O35 (`graph_shortest_path`'s `shortestPath` data model where the distance equals
  the bound), O36 (a `BETWEEN` on a composite index's lead next to a conjunct on a later
  field of it, per data model), O37 / O38 (the write IR: the data model redraws), O39
  (CONTAINSVALUE in a value subquery), O40 (the path IR's `rel:asc` at distance 3: a
  later `unionall` part with a nested `IN (subquery)` answers nothing), O5's
  mixed-type face (a select-IR join over the base class whose correlation would
  compare a sibling class's same-named property of another type through a throwing
  pair -- DECIMAL vs LONG, a LIST on the left, ...: `known_bugs.mixed_compare_throws`;
  the LET runs before `WHERE @class`), O5's sibling faces (cov400: a subclass query a
  BASE-class index serves filters the siblings' records before the class -- a
  `BETWEEN` under an OR on a sibling's other-typed field throws,
  `known_bugs.o5_sibling_filter` in `node_case_known`; a `poly` query converts its pivot
  through a sibling's index on a same-named field of another type,
  `o5_poly_sibling_index`; `CONTAINSVALUE` through a hash or DICTIONARY `BY VALUE`
  map index is a 501, `map_value_index_known`), O41 (DISTINCT, above), O45 (cards only: a MATCH pattern rooted at a later alias -- the class-carrying alias with the smallest record count, halved by a `where:`, ties to the first -- is walked backwards and an `outE('x')` / `inE('x')` / `bothE('x')` step crossed that way binds edges of EVERY class; `out('x')` keeps it, and the fuzzer's own MATCH concrete syntaxes step with `out`: `cards/known_bugs._o45_match`), O43 (a list
  `IN` over the doc data model's edge attributes: a single-element listed list is unwrapped
  and tested by containment, everywhere, and the undeclared list narrows it to int --
  the `rw` rewrite of a list `=`), O44 (above); the `index:` door shares O4 / O10 / O16 / O20 / O21 / O27
  (`index_door_known`); a reversed or arithmetic rewrite inherits only the scan-side
  findings (O7, O17, O27, O32: `known_bugs._plain` skips it in every index rule). Every guard looks at the LEAVES of
  a predicate tree; a pulled-up data model is judged on the query without its source
  predicate -- except the pulled-up `rel:flat`, which keeps it in `id IN (SELECT id FROM
  src WHERE p)`, an index-served query whose findings apply (cov400, O16). `--no-known-bugs` re-runs them. Every guard was paid for by a campaign
  bundle or a probe; extend `ganda/orientdb/scripts/probe_osql.py` with the fact before adding one.
- **The SELECT IR's facts** (`ganda/orientdb/scripts/probe_osql.py`, "the SELECT IR"): `count(*)` /
  `count(f)` over no rows answer 0 and every other aggregate answers NO row (`max`
  over rows that are all null is a null row, `sum` 0), so `:order` needs a present
  value among the matches and a distinct count a present one; a subquery target keeps
  the class's collation, date and decimal semantics and definedness, is never
  index-served and SORTS an ORDER BY (O18's empty DESC walk fires under a WHERE on
  another field too, and not over a subquery: `select_shape_known` judges O18 / O23
  on `table` / `poly` targets only). An index with `ignoreNullValues: true` holds no
  entry for a record with a null in ANY of its fields, so the `index:` door's prefix
  key of a composite misses such matches (the planner scans the prefix): the door is
  asked only where every match is complete (`concrete_syntax.support.door_complete`, sel-smoke3 seed
  1066). A date arithmetic rewrite past year 9999 is no shift (`_arith_exact`, sel-smoke4
  seed 1101).
- **The write IR's facts** ("the write IR" in `ganda/orientdb/scripts/probe_osql.py`): UPDATE / DELETE
  answer `[{"count": n}]`, `RETURN BEFORE|AFTER $current.id AS id, $current.f AS v` a
  row per record; a failed statement keeps the rows it already wrote (no statement
  atomicity outside a transaction) and a UNIQUE index is checked PER ROW, so a
  column a unique or dictionary index of any data model covers is written through one
  record only (`generators.write.unique_safe`, which also checks every key after the
  write); `SET f = f + :d` is `d` on a null f (O30), so an arithmetic write filters
  `f IS NOT NULL`; UPSERT only by `id` (on another predicate it inserts a record
  with no id); `DELETE FROM` a vertex / edge class holding records is refused
  (`DELETE VERTEX` cascades; the junction rows go first, through `IN (SELECT id
  ...)`); `DELETE FROM` a doc record leaves its incoming LINKs dangling, so the doc
  cascade rewrites them first; an element of an embedded list cannot be set in place
  (the doc data model's edge-attribute write re-sets each source's whole list by id,
  `Ref`s resolved through the session's RIDs); DELETE takes no TIMEOUT clause; a
  TIMEOUT leaves an UPDATE's partial writes committed. O37: an UPDATE served by an
  SB-tree index walk meets the records it moved (`SET f = f + 1 WHERE f > 4` never
  terminates) -- every UPDATE the fuzzer sends carries `TIMEOUT 10000 EXCEPTION`,
  and `known_bugs.o37_write` makes the data model redraw (`nest`, `tx`, a reversed filter).
  O38: in a transaction, a link list rewritten through `target.id` plus a delete of
  a dropped target fail at COMMIT (the doc cascade's `eids` form avoids it). O39:
  `CONTAINSVALUE` inside `IN (SELECT ...)` / `NOT IN` / a LET subquery is an NPE
  (guarded in the traversal LET / `rel:flat` concrete syntaxes, `node_in_subquery`,
  `tx_read`, the write `nest` target and the rel delete cascade).
- **A LET runs for every record of the target before the WHERE** (LET precedes
  WHERE): over the base class it reaches a sibling class's record, and comparing
  two declared types can throw (O5's mixed-type face: DECIMAL = LONG, a ranged
  DECIMAL vs DATETIME / BOOLEAN, DATETIME vs BOOLEAN, a stored LIST vs a scalar;
  every other pair converts the right operand, O32). A doc-side
  TRAVERSE over a link list counts hops (`TRAVERSE e1, target`: the embedded link
  document adds no depth). Every select kind answers through the transaction door's
  `RETURN $r`; O31 hits any of them through a one-sided range.
- **One HTTP session per database**: a session cookie outlives a dropped database and
  answers 401 for everything after.
- **Coverage** (`ganda/orientdb/engine/coverage.py`, README "Code coverage"): the JaCoCo agent rides
  in on `JAVA_OPTS`, `output=tcpserver,address=*` (loopback is unreachable through the
  port forward); every window is a DUMP with reset. The agent writes unbuffered behind
  Nagle, so a dump stalled on the 40 ms delayed ACK until the client learned to answer
  each chunk with a no-op header block (~5 ms now; keep `_AckingReader`). The JavaCC
  lexer `OrientSqlTokenManager` is excluded (a method too large once probed). A unique
  count carries one-time initialization onto whichever template runs first.
- **Large data (`run --scale large`; the bounds in `engine/limits.py`, each documented or
  probed on 3.2.57).** A BINARY column takes base64 text -- a bind parameter, a CONTENT
  document, a MERGE literal -- and answers the same standard base64 text; an EMPTY byte
  string is stored as NULL, so the model's pool holds none; `b = :p` with a base64
  pivot matches NOTHING on a scan and matches through an index (O46,
  `issues/O46-binary-equality-scan-vs-index.md`: `OQueryOperatorEquals.equals` decodes
  the text to a byte[] and then compares the arrays by identity; the index key is the
  decoded bytes compared by content), so a BINARY column takes no comparison predicate
  and no index in the model -- the divergence is on record there and in the probe, not
  in a campaign. An SB-tree index key is at most 10240 bytes, the

  value's UTF-8 form plus 16 (`OTooBigIndexKeyException`; a hash index has no bound):
  a grown value on an indexed field stays under `max_index_key_bytes`. The REST body is
  bounded by `network.http.maxLength` (1 MB in the image): every server the campaign
  starts runs with 64 MB (`HTTP_MAX_LENGTH`), a request stays under half of it, and a
  server attached through `ORIENT_URL` must be started with it. The bulk door
  (`statements_per_request`): a run of data steps travels as `BEGIN ... COMMIT` scripts,
  `/batch` requests, or -- the plain door -- NON-transactional scripts whose statements
  the engine auto-commits one by one (500 `LET $c = INSERT` statements in 0.15 s), the
  captures re-read 500 ids per `IN` lookup. A `TIMEOUT ... EXCEPTION` that fires
  (`OTimeoutException`) and an HTTP read timeout while the server still answers are
  TIMEOUTS: no answer, left out of the comparison, never a rejection. The stored state
  is checked by `count(*)` / `sum(links.size())` and samples read by `id IN :ids` /
  `out.id IN :ids` / `src_id IN :ids`, the doc data model's link targets resolved 500
  per `@rid IN :r`.
