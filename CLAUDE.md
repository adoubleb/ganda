# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

Ganda: a multi-model metamorphic fuzzer with an engine-agnostic core (`ganda/core`) and
one provider per DBMS (`ganda/orientdb` full; `ganda/hana` full, verified live against
HANA Cloud 4.00.000.00; `ganda/arangodb` full, verified live against ArangoDB 3.12.11;
`ganda/surrealdb` skeleton). `README.md` has the architecture, the provider contract and the glossary
that maps the legacy ports' terms to the ones used here. The legacy fuzzers
(`../OrientDB`, `../surrealDB`, `../arangoDB`) are reference material: do not edit them.

## Commands

```bash
pytest tests/ -q -n auto                           # no Docker (~20 s); whole offline HANA campaigns: tests.hana.run_offline
python3 -m ganda list
python3 -m ganda orientdb probe                    # Docker (or ORIENT_URL / ORIENT_PASSWORD)
python3 -m ganda orientdb run --iterations 20 --seed 1000 --workers 2
python3 -m ganda orientdb mutate --trials 8 --seed 1 --tag before-change
python3 -m ganda surrealdb doctor
python3 -m ganda arangodb probe                    # Docker (or ARANGO_URL / ARANGO_PASSWORD); ARANGO_IMAGE picks the image
python3 -m ganda arangodb run --scale large --iterations 4 --seed 1 --workers 4   # ArangoDB at the large scale
python3 -m ganda orientdb limits                   # what the engine can hold; the large profile on it
python3 -m ganda orientdb run --scale large --iterations 3 --seed 1   # the storage-engine stress mode
```

## Rules of the core

- **`ganda/core` imports no provider**, and providers do not import each other
  (`tests/test_layering.py`). The core reaches the engine through `Schema.engine` (set
  by `data_generator.build(seed, cfg, engine)`), `QueryCase.engine`, or an explicit
  argument (`predicates.evaluate(values, p, semantics)`, `draw(..., engine)`). No
  global engine state.
- **Engine behaviour never lives in the core.** If a line of core code is only true
  for one engine (what `null + 1` is, where nulls sort, which index kind is unique), it
  belongs behind a slot of `core/engine/provider.py`. Add the member to the contract's
  docstring, to `tests/test_layering.py`'s slot list and to `tests/fake_engine`.
- **Every consumer draws from its own named stream** (`core/rng.py`), so adding a
  draw in one place never shifts another; a new axis draws from a new tail stream
  behind a probability that defaults to 0. Ganda no longer tracks parity with the
  legacy OrientDB fuzzer: old campaigns are reproduced with `../OrientDB` itself.
- **Query generator ids are random-stream names** (`ir_traversal`, `ir_select`,
  `ir_path`, `ir_write`): renaming one changes every seed.
- **A provider facade binds the engine** so provider code and tests can call core
  functions without naming it: `ganda/orientdb/facade/` (`predicates.py`,
  `generate.py`) and `ganda.orientdb.Schema`. Core code never uses these.
- **Provider layout** (OrientDB, ArangoDB, HANA alike): `provider.py` and `config.py`
  at the top; `engine/` (connection, limits), `storage/` (dialect, physical design,
  `mapping/`), `oracle/` (semantics, pivots, rewrites), `concrete_syntax/`,
  `known_bugs/` (`guards.py`), `mutation/` (`mutants.py`), `scripts/`.
- **Use conventional names** (the README glossary). New concepts get the textbook term.
- **Engine limits are the provider's, in its `limits` slot** (`core/data/limits.py`,
  `ganda/orientdb/engine/limits.py`: every number documented or probed). The large-data axes
  (`large_row_probability`, `binary_probability`, `statements_per_request`) and the
  `large` scale profile (`core/data/scale.py`) stay inside them; the core never hard-codes
  a size an engine would reject. A new axis of the kind draws from a tail stream and
  defaults to off.
- **The oracle is purely differential.** No answer is predicted from the Instance: a
  case compares its variants pair by pair, and the state check compares the data
  models' stored states with each other (the one off the consensus has the problem;
  a tie names every one). A fault every data model shares is out of reach by design.
  The IRs walk the Instance only to steer generation (filters that keep records,
  writes that match some, exact sums, `write.apply` for later draws).
- **At the large scale** the state check samples (`--state-sample`), a
  timeout is no answer (`CaseResult.timeouts`; at the default scale it stays the
  error asymmetry it was, the finding seed 1010 has always carried), and bundles are
  slim. A `BINARY`

  column is a blob: written, stored, projected and read back, never compared or
  indexed (`SCALAR_TYPES` excludes it; `predicates.draw` answers only the null tests).

## OrientDB

The engine rules are minimal records in `ganda/orientdb/rules/catalog.py`, as HANA's (the
subjects are OrientDB SQL's execution paths: a scan, an index-served statement, MATCH,
TRAVERSE, the script / transaction door, /batch, the `index:` door); the defects O1..O46
beside them; `rules/checks.py` lists the rules each probe check (C1..C330 in
`scripts/probe_osql.py`) asserts. `ganda orientdb rules --write` renders
`ganda/orientdb/rules/RULES.md`; `tests/orientdb/test_rules.py` keeps them in step. Layout: `storage/dialect.py` (vocabulary), `storage/physical_design.py`, `config.py`,
`engine/limits.py` (what the engine can hold), `engine/connection.py`, `storage/mapping/`, `oracle/semantics.py`, `oracle/pivots.py`, `oracle/rewrites.py`,
`concrete_syntax/` (`predicate`, `traversal`, `select`, `path`, `write`, `support`),
`known_bugs/guards.py` (most of O1..O46; O3, O22, O24, O29, O30, O34, O42, O45 and O46 are avoided by the renderers or the core's value pools instead, O35 and O40 guarded in `concrete_syntax/path.py`) with `known_bugs/java_hash.py`, `mutation/mutants.py`, `engine/coverage.py` (JaCoCo),

`scripts/` (`probe_osql.py`, `coverage_report.py`).

Adding a concrete syntax: a choice in the catalog of its module (`SYNTAXES` and
`_RENDERERS` for a traversal; an axis of `SelectSyntax`), what it admits in
`applicable`, its text in `render`, and its judgement in `known_bugs`. Then `pytest`,
the probe if a new fact is relied on, a campaign, and the mutation benchmark A/B over
the same seeds.

## ArangoDB

`ganda/arangodb/RULES.md` holds the engine rules the oracle rests on, each marked
documented / legacy-probed / probed (`ganda arangodb probe`, fact ids `V` values, `S`
statement forms, `W` write doors, `L` limits, `T` timeout, `A` findings pinned). Layout
mirrors OrientDB's: `storage/dialect.py` (the logical `id` attribute, the percent-encoded
`_key`, `engine_id` / `logical_id`), `storage/physical_design.py` (persistent indexes, unique
or sparse; the expanded `f[*]` array index for a LIST field; no signed zero in the
pools), `config.py`, `engine/limits.py`, `engine/connection.py` (the cursor API, `bind_subset`, stream
transactions, `maxRuntime` as the timeout), `engine/tx.py` (the transaction door's script
text), `storage/mapping/` (bulk inserts chunked under half the request bound, `mergeObjects:
false` on every update door), `oracle/semantics.py` (the total type order, exact numbers, code
point string equality, `LIKE` as an ICU regex), `oracle/pivots.py` (`ordered_ops`: a string
column takes the equality family only -- ICU collation orders them), `oracle/rewrites.py`,
`concrete_syntax/` (`predicate`, `traversal`, `select`, `path`, `write`, `support`),
`known_bugs/guards.py` (A40 guarded), `mutation/mutants.py`, `scripts/probe.py`.

Campaigns run on mousedeer (`ssh mousedeer`, a ControlMaster socket the user opens with
`ssh -fN mousedeer`; the tree is rsynced to `/home/hakam/ganda/ganda`, the image there is
`arangodb:3.12.11`, so `export ARANGO_IMAGE=arangodb:3.12.11`); the probe and the unit
tests run anywhere. The legacy port `../arangoDB` is reference material (its A1..A39
are in `../arangoDB/issue-drafts/`); this port's findings go under `issues/` (A40 on).

## Live verification of ArangoDB (2026-10-09, ArangoDB 3.12.11 on mousedeer)

- `ganda arangodb probe`: 25 facts hold (`RULES.md` lists them with their status); the
  value facts V2, V3 and V5 are MIRROR checks of `oracle/semantics.py` against the engine over
  boundary pools, which is how three assumptions fell (numbers compare exactly, `LIKE`
  crosses a newline, two nulls are not ordered).
- `run --iterations 3 --seed 1` at the default scale with every axis on: 707 cases, 0
  findings, 0 symmetric or internal errors, 0 setup asymmetries; A40 withheld on 87
  variants. The smokes before it found five faults of the port (the door's collections,
  `mergeObjects`, the hint on a sparse or composite index, the base class in a semi-join,
  `VALUES()` on a null map) and the engine inconsistency A40.
- `run --scale large --iterations 4 --seed 1 --workers 4` with every axis on (seeds of
  7645 / 10181 / 5239 / 10077 nodes and 16000 / 13649 / 35015 / 42332 edges, hubs of up
  to 6456 edges, up to 242 grown values and 3.3 M string characters, up to 6 binary
  columns, three with the base class): 254 cases, 92.1 % asserting, 0 setup asymmetries,
  0 write-state diffs, 16 writes, 30 timeouts (the doc data model's incoming hops scan
  the sources per start record), 3824 s on one server shared by the four workers. Its
  one bundle was a cursor whose time to live expired between batch fetches of a
  self-join's millions of ids (fixed: an hour of `ttl`, errorNum 1600 read as no answer).
  The first attempt at this profile sat for an hour in one stream transaction of a
  hundred thousand single-statement requests (fixed: `fold_steps` and a transaction per
  chunk) and found **A41** on seed 3.
- `mutate --trials 3 --seed 1`: 17 of 18 mutants caught, mean rate 0.944 (the miss is
  `graph_stale_edge_update`, which needs edge-update churn); with
  `--churn-edge-update-probability 0.2`: 16 of 18, mean 0.833 -- the two structural
  misses drop edges whose EdgeUpdate churn then updates a missing document, which fails
  the mutant's SETUP (errorNum 1202), and the runner skips rather than scores a trial
  whose setup fails.

## SAP HANA

The engine rules are minimal records in `ganda/hana/rules/catalog.py` (`ganda/core/rules/`):
`Rule(question, subject, answer)`, the question's kind saying what the fuzzer does with
it (compare, ask, equivalent, exact); the defects A1-A10 and the error classes beside
them. `rules/checks.py` lists the rules each probe fact's check asserts; the rest are
unchecked. `ganda hana rules --write` renders `ganda/hana/rules/RULES.md`, and
`tests/hana/test_rules.py` keeps the catalog, the checks, the probe, `connection.py`'s
error codes and the rendered file in step. Data models
`doc` (JSON Document Store), `rel` (column tables), `graph` (Property Graph Engine: a
GRAPH WORKSPACE over views of rel-shaped
tables, openCypher through `OPENCYPHER_TABLE`; added 2026-10-11, its 32 `G*` probe facts
run live the same day) and `rdf` (Knowledge Graph Engine, SPARQL). The provider attaches
to a HANA Cloud instance (`HANA_HOST`, `HANA_PORT`, `HANA_USER`, `HANA_PASSWORD`); there
is no container.

The graph data model has two syntax families (`concrete_syntax/graph.py`): rel's SQL
over `g_` CTEs that read every table through the engine (the views family: full
coverage), and native openCypher where the language states the question. What the probe
fixed (`accepts.match.second_clause`, `match.edge_binding`, `order.nulls.asc` in the
catalog): a heterogeneous workspace takes ONE MATCH clause of one-edge comma-separated
patterns, and a relationship binds once in it, so a native
multi-hop is asked only over pairwise distinct edge types (the other data models' walk
semantics has no other native form); cypher's ORDER BY puts NULL last ascending (HANA SQL: first),
so native `page` / `keys` are not asked; parameters bind bare only; an undirected
self-loop matches once; SUM over nothing answered NULL (the docs say 0: every native sum
carries its COUNT and SQL decides). The switches in `graph.py` are named after the facts.

Added 2026-10-12 (each with catalog rules and probe facts): plan steering
(`concrete_syntax/hints.py`: every SQL text asked once more under one of sixteen `WITH
HINT (...)` profiles -- HEX off or on, ESX, OLAP, the join engine, row vs column operators, the
optimizer levels, the rewrites off, HEX's disk operators and join kinds, no indexes,
single-threaded -- one profile per question, the variants `rel:<profile>` /
`graph:nest+<profile>` / `rdf:table_fn+<profile>` / `rel:hint_<profile>`; a hinted twin
that answers differently is an engine defect with no cross-model confound); the Document
Store's `NOT` kept as written (`doc:neg`); the custom-key axis; and the inheritance axis
(the base class `N0` as a UNION ALL view on rel and the graph data model, the views'
union on doc, a second class triple on rdf, the unlabelled `MATCH (a)` in openCypher; a
write on it one statement per subclass, the counts summed). SQLite has no hints, so plan
steering is compared live only (the offline session strips the clause).

The 12-hour campaign of 2026-10-12 (its first ninety minutes, 25 bundles) added **A8** (a
DOUBLE constant in a cypher text is parsed to ~15 significant digits; bound doubles are
exact) and **A9** (two OPENCYPHER_TABLE calls in one statement type the second call's bare
parameters in the wrong order when they mix a string and a number): the graph renderings
now bind DOUBLE pivots only and inline every other pivot as a constant, and the `literal`
variant is gone. Also found: HANA SQL's `AVG(BIGINT)` is `DECIMAL(25,6)` TRUNCATED to six
places while the triple store's AVG computes in double (B13,
guarded in `known_bugs`); a BOOLEAN is refused inside COUNT and in a comparison (B14).

The oracle on HANA runs with two provider options the core added 2026-10-12 and the
other providers keep off: `partial_traversals`
(a traversal the doc views cannot ask stays a partial case instead of being dropped
everywhere) and `compare_despite_errors` (the variants that answered are compared when
another one errored; the finding key names the mismatching pairs and the error's code
through `error_signature`). Cross-type numeric pivots and `NOT IN` are drawn
(`hana/oracle/pivots.py`), each typed per data model (`exact.cross_type_numeric_pivot.below`); no variant repeats
another's text on its data model; a decoding failure is an internal `EngineError`.
More engine paths per question (track 2, 2026-10-12): `rel:again` (the base text a second
time), the profile `plan_cache`, the hinted collection query, `rel:inline` (constants in the
text), `rel:block` / `rel:block_ni` (a SQLScript anonymous block, inlining on or off), the
form `tlp` (EXCEPT the false EXCEPT the unknown; MINUS with BIND in SPARQL), the
transaction door's peer check (a second connection must not see the junk) and the path's
`graph:script` (GraphScript's `Shortest_Path` over a keys-only workspace `gws`). The
stand-in skips the GraphScript objects, unwraps blocks and strips hints; those three are
compared live only.

Verified live against HANA Cloud 4.00.000.00 (2026-10-09): the probe is 57/57, the first
campaigns run, and six engine defects are written up with repros under
`ganda/hana/issues-draft/` -- **H1** (an `xsd:double` is serialized to ~6 significant
figures in SPARQL results though stored exactly), **H2** (the Document Store crashes
on a correlated scalar subquery over an UNNEST view), **H3** (SPARQL SUM / AVG turn unbound
over a group with an unbound value once the predicate has held a second datatype anywhere
in the store), **H4** (a SUM over collection views overflows on rows a JOIN excludes),
**H5** (`BETWEEN ? AND ?` over a collection field types its parameters BIGINT) and **H6**
(a legal SPARQL FILTER / BIND over a variable bound nowhere is refused); the provider works
around them and withholds H4's shape (`known_bugs/guards.py`). `ganda/hana/findings/README.md`
sorts every live observation into A (engine bug), B (quirk or documented) and C (provider
bug), each B with a verdict checked against SAP's docs. HANA reports EVERY SPARQL refusal
(a syntax error too) as code 129, so a 129 carrying an ordinary refusal is a rejection, not
an internal error (`connection.REJECTION_129`).

If the instance is reached through an SSH tunnel to a bastion, set `HANA_SNI_HOSTNAME` to
the real hostname (HANA Cloud routes TLS by SNI) and leave `disableCloudRedirect` on (its
default when the SNI is set): HANA Cloud otherwise redirects the session off the tunnel to
a direct address. `connection.connect()` retries a lost connect (`HANA_CONNECT_RETRIES`).

```bash
pytest tests/hana tests/test_layering.py -q        # no server (test_offline needs rdflib)
python3 -m tests.hana.run_offline --seeds 1 10     # whole campaigns on SQLite + rdflib through the real session code
python3 -m ganda hana doctor
python3 -m ganda hana rules                        # the engine rules as tables; --write renders ganda/hana/rules/RULES.md
python3 -m ganda hana probe                        # 141 facts (R/D/K 2026-10-10, G 2026-10-11, P and the 2026-10-12 facts); `probe K5 D` for a subset
```

After any change to a HANA renderer, mapping or semantics: the offline runner first (it
catches logic mistakes without HANA), then the probe and a small live campaign (the tunnel
is ~180 ms/statement, so a full campaign is 20-30 min; start with `--select-cases 8
--traversal-cases 4 --write-cases 2`). A probe fact that fails changes the provider, never
the probe -- unless the probe's own SQL tested a form the provider never emits.

The offline stand-in answers rdf the way HANA does (`tests/hana/offline.py`, `_as_hana`):
a double term rounded to `%g` (H1) and, through SPARQL_TABLE, a stored long / double /
boolean as text (HANA types a column after the datatypes its predicate holds across the
graph). So a value projection missing its decode suffix or its `STR` fails offline, not
live. The graph data model runs offline through `tests/hana/opencypher_sqlite.py`, a
translator of exactly the openCypher subset the renderers emit into SQLite (its switches
mirror the engine's documented semantics; a construct it lacks makes the variant partial
offline, never an error). The provider bugs of campaign-20261009-095838
(`findings/README.md`, C4-C9) are pinned in `tests/hana/test_units.py`.

## Triage of a finding

`bugs/<tag>-<seed>-<generator>-<shape>/` holds `README.md`, `queries.json` (the
engine, every variant's text, bind variables, rows, mismatches), one
`<data model>_setup.json` plan each, `instance.json` and `repro.py` (which calls
`ganda.core.campaign.replay`). Read `failing_pairs` first: a same-database pair
(`graph/graph:match`) is a within-engine inconsistency with no cross-model confound.
`internal_error` is a raw exception from inside the engine and is a finding on its own.
A triaged finding is written up under `issues/` (`O46-...md`: the version, a minimal
reproduction, expected and actual, the analysis down to the engine's source, the scope,
how Ganda met it), ready to file; the legacy drafts O1..O45 are in
`../OrientDB/issue-drafts/`.


## Live verification (2026-10-05, OrientDB 3.2.56 in Docker)

Run side by side with the same commands in `../OrientDB`; repeat after any change to
the connection layer, the campaign, the oracle or the write phase:

- `ganda orientdb probe`: every fact holds.
- `run --iterations 12 --seed 1000 --workers 2` with every axis on: all 28 statistics
  fields equal the legacy run's (2834 cases, the same known-bug counts, the same one
  finding on seed 1010), and the bundle's `repro.py` replays it.
- `run --iterations 2 --seed 1 --no-known-bugs`: the same 49 finding keys and 47 bundles.
- `mutate --trials 3 --seed 1 --inheritance-probability 0.5` over 10 mutants (instance,
  render and index ones): per-mutant results identical to the legacy runner's.
- `run --coverage variant --workers 1` and `scripts/coverage_report.py report`.
- `ganda surrealdb doctor`, `ganda arangodb doctor`, `ganda orientdb doctor`.

Not run: the full 21-mutant benchmark at 8 trials, and long campaigns.

## Live verification of the large scale (2026-10-07, OrientDB 3.2.57 in Docker)

`orientdb:latest` is now 3.2.57, and FOUR pre-existing facts changed on it: the probe's
O4 check (an equality through a DICTIONARY index answers every record) and three O18
checks (a DESC walk of an index that ignores nulls answers rows) now fail, in the legacy
tree's probe as well -- the engine, not the port. The O4 / O18 guards therefore withhold
variants 3.2.57 answers correctly; re-probe and decide whether to retire them or pin the
image. Everything else below held.

- `ganda orientdb probe`: every other fact holds, the new "large data" section included
  (BINARY round trip and its scan-vs-index equality, the 10240-byte index key, a 4 MB
  string / 3 MB binary / 200k-element list through the 64 MB body bound, 500-statement
  bulk scripts, bulk id resolution, timeouts).
- `run --iterations 12 --seed 1000 --workers 2` with every axis on: 2834 cases as
  recorded, 0 setup asymmetries, 96 writes, the one finding on seed 1010 (an error
  asymmetry that is a timeout, now also counted under `timeouts`).
- `run --scale large --iterations 1 --seed 7` at 300-600 nodes and 500-1500 edges per
  type with every axis on: 1466 nodes / 2556 edges in three data models, setup through
  500-statement scripts in seconds, state verified by counts and a sample, 64 cases, 4
  writes, 0 findings, 5 relational three-hop variants timed out (60 s) and left out,
  468 s in all.
- `run --scale large --iterations 1 --seed 1` at the full profile with every axis on
  (7645 nodes, 16000 edges, a 5717-edge hub, 82 grown values, 4.7 MB of strings, 5
  binary columns, per data model): the three setups and state checks in about six
  minutes, 64 cases (95 % asserting), 4 writes, 0 findings, 3 timeouts, 935 s in all.
  Offline the same seed builds in 3.7 s and renders each plan in under a second; the
  10k-row seed 2 draws its 52 cases in 1.9 s (45 s before the `IN` index in
  `oracle/semantics.py`, and minutes before `sums_exactly` learned to count paths).



