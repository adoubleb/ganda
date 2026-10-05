# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

Ganda: a multi-model metamorphic fuzzer with an engine-agnostic core (`ganda/core`) and
one provider per DBMS (`ganda/orientdb` full; `ganda/surrealdb`, `ganda/arangodb`
skeletons). `README.md` has the architecture, the provider contract and the glossary
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

## OrientDB

`ganda/orientdb/RULES.md` holds the engine rules that keep the oracle sound (each one
probed live). Layout: `dialect.py` (vocabulary), `physical_design.py`, `config.py`,
`connection.py`, `mapping/`, `semantics.py`, `pivots.py`, `rewrites.py`,
`concrete_syntax/` (`predicate`, `traversal`, `select`, `path`, `write`, `support`),
`known_bugs.py` (O1..O45) with `java_hash.py`, `mutants.py`, `coverage.py` (JaCoCo),
`scripts/` (`probe_osql.py`, `coverage_report.py`).

Adding a concrete syntax: a choice in the catalog of its module (`SYNTAXES` and
`_RENDERERS` for a traversal; an axis of `SelectSyntax`), what it admits in
`applicable`, its text in `render`, and its judgement in `known_bugs`. Then `pytest`,
the probe if a new fact is relied on, a campaign, and the mutation benchmark A/B over
the same seeds. A new syntax changes the cases, so parity with the legacy tree ends
there on purpose: record that in the commit.

## Triage of a finding

`bugs/<tag>-<seed>-<generator>-<shape>/` holds `README.md`, `queries.json` (the
engine, every variant's text, bind variables, rows, mismatches), one
`<data model>_setup.json` plan each, `instance.json` and `repro.py` (which calls
`ganda.core.replay`). Read `failing_pairs` first: a same-database pair
(`graph/graph:match`) is a within-engine inconsistency with no cross-model confound; an
empty list with `reference_mismatches` means every database agreed and only the
reference disagreed, so check the provider's semantics before blaming the engine.
`internal_error` is a raw exception from inside the engine and is a finding on its own.

## Not yet verified live

The port was verified offline: unit tests, the fake-engine end-to-end tests and
byte-for-byte parity of everything generated. Docker was not available when it was
built, so these have NOT run against a server and are the first thing to check:

- `ganda orientdb probe` and a campaign (`run`), against the same command in
  `../OrientDB`: per-generator case counts, shape census and findings should match.
  The touched live paths are `OrientSession.execute` / `read_nodes` / `read_edges`,
  `concrete_syntax/write.execute`, the campaign's crash and coverage handling.
- `ganda orientdb mutate --trials 8 --seed 1` against the legacy mutation reports.
- `ganda orientdb run --coverage variant --workers 1`.
- `ganda surrealdb doctor`, `ganda arangodb doctor` (new connection code).
