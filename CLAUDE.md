# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

Ganda: a multi-model metamorphic fuzzer with an engine-agnostic core (`ganda/core`) and
one provider per DBMS (`ganda/orientdb` full; `ganda/hana` full, verified live against
HANA Cloud 4.00.000.00; `ganda/surrealdb`, `ganda/arangodb` skeletons). `README.md` has the architecture, the provider contract and the glossary
that maps the legacy ports' terms to the ones used here. The legacy fuzzers
(`../OrientDB`, `../surrealDB`, `../arangoDB`) are reference material: do not edit them.

## Commands

```bash
pytest tests/ -q                                   # no Docker: core on the fake engine, OrientDB units, parity slice, layering
python3 scripts/golden_dump.py --check             # full parity with the legacy OrientDB fuzzer (~25 s)
python3 -m ganda list
python3 -m ganda orientdb probe                    # Docker (or ORIENT_URL / ORIENT_PASSWORD)
python3 -m ganda orientdb run --iterations 20 --seed 1000 --workers 2
python3 -m ganda orientdb mutate --trials 8 --seed 1 --tag before-change
python3 -m ganda surrealdb doctor
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
  belongs behind a slot of `core/provider.py`. Add the member to the contract's
  docstring, to `tests/test_layering.py`'s slot list and to `tests/fake_engine`.
- **RNG draw order is frozen.** Every consumer draws from its own named stream
  (`core/rng.py`). A provider hook is called exactly where the code it replaced drew;
  a new axis draws from a new tail stream behind a probability that defaults to 0.
  `scripts/golden_dump.py --check` is the gate: run it after any change to the data
  generator, an AST, a query generator or OrientDB's concrete syntax, known bugs or
  semantics. If it fails, `--dump <profile> <seed>` on both trees and diff.
- **The parity baseline is only re-recorded on purpose** (`--legacy ../OrientDB
  --write`), when a rename needs a new entry in the script's rename map. A behaviour
  change that breaks parity is a decision to say out loud, not something to re-record
  away.
- **Query generator ids are random-stream names** (`ir_traversal`, `ir_select`,
  `ir_path`, `ir_write`): renaming one changes every seed.
- **A provider module that binds the engine** exists so provider code and tests can
  call core functions without naming it: `ganda/orientdb/predicates.py`,
  `generate.py`, `ganda.orientdb.Schema`. Core code never uses these.
- **Use conventional names** (the README glossary). New concepts get the textbook term.
- **Engine limits are the provider's, in its `limits` slot** (`core/limits.py`,
  `ganda/orientdb/limits.py`: every number documented or probed). The large-data axes
  (`large_row_probability`, `binary_probability`, `statements_per_request`) and the
  `large` scale profile (`core/scale.py`) stay inside them; the core never hard-codes
  a size an engine would reject. A new axis of the kind draws from a tail stream and
  defaults to off; a new dataclass field that the legacy tree lacks goes into
  `scripts/golden_dump.py`'s `GANDA_ONLY_FIELDS` so parity still hashes the behaviour.
- **At the large scale the reference leg is off by design** (the profile's
  `reference=False`; `--reference` turns it back on): the oracle is the pairwise
  comparison of the data models, the state check samples (`--state-sample`), a
  timeout is no answer (`CaseResult.timeouts`; at the default scale it stays the
  error asymmetry it was, the finding seed 1010 has always carried), and bundles are
  slim. A `BINARY`

  column is a blob: written, stored, projected and read back, never compared or
  indexed (`SCALAR_TYPES` excludes it; `predicates.draw` answers only the null tests).

## OrientDB

`ganda/orientdb/RULES.md` holds the engine rules that keep the oracle sound (each one
probed live). Layout: `dialect.py` (vocabulary), `physical_design.py`, `config.py`,
`limits.py` (what the engine can hold), `connection.py`, `mapping/`, `semantics.py`, `pivots.py`, `rewrites.py`,
`concrete_syntax/` (`predicate`, `traversal`, `select`, `path`, `write`, `support`),
`known_bugs.py` (O1..O45 guarded; O46 documented, no guard) with `java_hash.py`, `mutants.py`, `coverage.py` (JaCoCo),

`scripts/` (`probe_osql.py`, `coverage_report.py`).

Adding a concrete syntax: a choice in the catalog of its module (`SYNTAXES` and
`_RENDERERS` for a traversal; an axis of `SelectSyntax`), what it admits in
`applicable`, its text in `render`, and its judgement in `known_bugs`. Then `pytest`,
the probe if a new fact is relied on, a campaign, and the mutation benchmark A/B over
the same seeds. A new syntax changes the cases, so parity with the legacy tree ends
there on purpose: record that in the commit.

## SAP HANA

`ganda/hana/HANDOFF.md` is the guide (setup, the order of live work, where each probe
fact lives in the code); `ganda/hana/RULES.md` lists the engine facts with their status
(documented, assumed, probed). Data models `doc` (JSON Document Store), `rel` (column
tables), `rdf` (Knowledge Graph Engine, SPARQL). The provider attaches to a HANA Cloud
instance (`HANA_HOST`, `HANA_PORT`, `HANA_USER`, `HANA_PASSWORD`); there is no container.

Verified live against HANA Cloud 4.00.000.00 (2026-10-09): the probe is 57/57, the first
campaigns run, and two engine defects are written up with repros under
`ganda/hana/issues-draft/` -- **H1** (an `xsd:double` is serialized to ~6 significant
figures in SPARQL results though stored exactly) and **H2** (the Document Store crashes
on a correlated scalar subquery over an UNNEST view); the provider works around both.

If the instance is reached through an SSH tunnel to a bastion, set `HANA_SNI_HOSTNAME` to
the real hostname (HANA Cloud routes TLS by SNI) and leave `disableCloudRedirect` on (its
default when the SNI is set): HANA Cloud otherwise redirects the session off the tunnel to
a direct address. `connection.connect()` retries a lost connect (`HANA_CONNECT_RETRIES`).

```bash
pytest tests/hana tests/test_layering.py -q        # no server (test_offline needs rdflib)
python3 -m tests.hana.run_offline --seeds 1 10     # whole campaigns on SQLite + rdflib through the real session code
python3 -m ganda hana doctor
python3 -m ganda hana probe                        # 57/57 live; `probe K5 D` for a subset
```

After any change to a HANA renderer, mapping or semantics: the offline runner first (it
catches logic mistakes without HANA), then the probe and a small live campaign (the tunnel
is ~180 ms/statement, so a full campaign is 20-30 min; start with `--select-cases 8
--traversal-cases 4 --write-cases 2`). A probe fact that fails changes the provider, never
the probe -- unless the probe's own SQL tested a form the provider never emits.

## Triage of a finding

`bugs/<tag>-<seed>-<generator>-<shape>/` holds `README.md`, `queries.json` (the
engine, every variant's text, bind variables, rows, mismatches), one
`<data model>_setup.json` plan each, `instance.json` and `repro.py` (which calls
`ganda.core.replay`). Read `failing_pairs` first: a same-database pair
(`graph/graph:match`) is a within-engine inconsistency with no cross-model confound; an
empty list with `reference_mismatches` means every database agreed and only the
reference disagreed, so check the provider's semantics before blaming the engine.
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
  `semantics.py`, and minutes before `sums_exactly` learned to count paths).



