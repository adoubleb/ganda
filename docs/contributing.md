# Contributing

Read [architecture.md](architecture.md) first; this page assumes its vocabulary
([glossary.md](glossary.md) has every term).

## Setup

Python 3.11 or later. Docker for OrientDB, ArangoDB and SurrealDB (a container is started
for you). SAP HANA needs a HANA Cloud instance; there is no container.

```bash
pip install -e ".[dev]"            # pytest, rdflib
pip install -e ".[dev,hana]"       # plus hdbcli, for the HANA provider
```

Check the install with no server at all:

```bash
python3 -m ganda list
pytest tests/ -q
```

Then talk to a real engine:

```bash
python3 -m ganda orientdb doctor                 # one database per data model, one query each
python3 -m ganda orientdb probe                  # every engine fact the provider relies on
python3 -m ganda orientdb run --iterations 5 --seed 1
```

To attach to a server you started yourself instead of a fresh container, set the
provider's variables:

| Engine | Variables |
|---|---|
| OrientDB | `ORIENT_URL`, `ORIENT_PASSWORD` |
| ArangoDB | `ARANGO_URL`, `ARANGO_PASSWORD`, `ARANGO_IMAGE` (the container image, e.g. `arangodb:3.12.11`) |
| SurrealDB | `SURREAL_URL` |
| SAP HANA | `HANA_HOST`, `HANA_PORT`, `HANA_USER`, `HANA_PASSWORD` (and `HANA_SNI_HOSTNAME` through a tunnel) |

## Repository map

```
ganda/
  cli.py, registry.py      ganda <engine> <command>; engine name -> provider
  core/                    engine-agnostic: never imports a provider
    model.py               Schema, Instance, rows, churn
    data_generator.py      seeded generation; GeneratorConfig
    rng.py                 named random streams
    ir/                    predicates + traversal, select, path, write IRs (and the Instance walks generation steers by)
    generators/            query generators (ir_traversal, ir_select, ir_path, ir_write), write phase
    case.py, oracle.py     QueryCase, pairwise comparison, finding classes
    state_check.py         do the data models' stored states agree?
    campaign.py            the campaign loop (start with run_instance)
    dedup.py, reporter.py, replay.py
    mutation/, coverage.py, scale.py, limits.py, docker.py, setup_plan.py
    provider.py            THE CONTRACT
  orientdb/ arangodb/ hana/ surrealdb/     one provider each (same layout)
    provider.py            the slots
    RULES.md               engine facts the oracle rests on, each with its probe
    rules/                 (HANA) the same as typed records: catalog.py, RULES.md generated
    connection.py          Server, Session
    mapping/               Instance -> setup plan, per data model
    semantics.py, pivots.py, rewrites.py, physical_design.py, limits.py, config.py
    concrete_syntax/       predicate, traversal, select, path, write
    known_bugs.py, mutants.py
    scripts/probe*.py      `ganda <engine> probe`
tests/
  fake_engine/             an in-memory provider; the core's tests run on it
  core/ orientdb/ arangodb/ hana/
  test_layering.py         the import rules and the contract's slot list
scripts/
  campaign.py              tests -> probe -> campaign -> triage, in one command
bugs/ campaign_reports/ mutation_reports/   outputs (gitignored)
issues/                    triaged findings, written up ready to file
```

## The gates

Run the gates that match what you touched. Each one catches a different kind of mistake.

| Gate | Command | Run it when | Needs |
|---|---|---|---|
| Unit and layering tests | `pytest tests/ -q` (~15 min in full; narrow it with a path such as `tests/core` while iterating) | Always | Nothing |
| Probe | `python3 -m ganda <engine> probe` | You relied on a new engine fact, or upgraded the engine | Server |
| Campaign | `python3 -m ganda <engine> run --iterations 20 --seed 1000 --workers 2` | Any change that reaches the engine | Server |
| Mutation A/B | `python3 -m ganda <engine> mutate --trials 8 --seed 1 --tag before` then `--tag after` | A change to the query supply or the oracle | Server |
| HANA offline | `python3 -m tests.hana.run_offline --seeds 1 10` | Any change to a HANA renderer, mapping or semantics (before going live) | rdflib |

`scripts/campaign.py --tag <tag> --iterations N` runs tests, probe, campaign and triage
in order and exits 1 if the campaign found something.

**Comparing mutation reports:** compare catch rates per mutant over the same mutant set
and seeds. A headline mean across sets of different sizes means nothing.

## Rules

These are enforced by tests or by review. Most of them exist because breaking them
silently corrupted results in the past.

1. **The core imports no provider; providers don't import each other.** `tests/test_layering.py`
   fails otherwise.
2. **Engine behaviour lives in a provider slot.** If you find yourself writing
   `if engine == ...` in the core, or a rule only one engine follows, add a slot instead:
   document it in `core/engine/provider.py`'s docstring, add it to `tests/test_layering.py`'s slot
   list, and implement it in `tests/fake_engine` and every full provider.
3. **Never reorder random draws.** Draw from `rng.stream(seed, "<new name>")` for anything
   new. A new generation axis goes behind a probability that defaults to 0, so every
   existing seed reproduces exactly.
4. **Don't rename query generator ids** (`ir_traversal`, `ir_select`, `ir_path`,
   `ir_write`). They name random streams.
5. **Every engine fact is probed.** Before code relies on how the engine behaves, add a
   check to the provider's probe and a line to its `RULES.md` (HANA: a record in
   `rules/catalog.py`). If a probe fact fails,
   change the provider, not the probe.
6. **Respect the engine's limits.** Sizes come from the provider's `limits` slot; the
   core never hard-codes a size an engine would reject.
7. **Use conventional names.** "Concrete syntax", not "spelling"; "data model", not
   "side". The README glossary maps the legacy terms. New concepts get the textbook term.
8. **Don't edit the legacy fuzzers** (`../OrientDB`, `../arangoDB`, `../surrealDB`).
   They are reference material, and the way to reproduce
   their old campaigns: Ganda no longer tracks parity with them.

Commit messages start with the area they touch (`HANA: ...`, `ArangoDB: ...`,
`core: ...`) and say what changed in behaviour.

## How to...

### Add a concrete syntax to an existing provider

This is the most common contribution: another way of asking a question the engine
already gets asked, so the oracle gains one more engine path to compare.

1. Add the choice to the catalog in `<engine>/concrete_syntax/<ast>.py` (for a traversal,
   `SYNTAXES` and `_RENDERERS`; for a select, an axis of `SelectSyntax`).
2. Say which queries it can express in `applicable` / `syntaxes_for`, and write its text
   in `render`.
3. If it hits a filed bug at a known shape, withhold it in `known_bugs/guards.py`.
4. If its correctness relies on an engine fact nobody probed yet, add the probe and the
   `RULES.md` line (HANA: the `rules/catalog.py` record).
5. Run `pytest`, the probe, a campaign, and a mutation A/B over the same seeds.

The new syntax shows up as a variant `<data model>:<tag>` and is compared against every
other variant automatically. You don't touch the oracle.

### Add a rule to a provider's semantics

`oracle/semantics.py` holds the engine's value rules: the order and equality answers are
compared under, and the predicate logic and arithmetic the generators walk the Instance
with (to draw filters that keep records, writes that match some, sums that stay exact).
It predicts no answer.

1. Write a probe fact showing the engine's behaviour over boundary values.
2. Make `oracle/semantics.py` match it, and add the fact to `RULES.md` (HANA: `rules/catalog.py`).
3. If a question would part the data models for a legitimate reason (a collation one
   of them applies, say), narrow `ordered_ops` or the generator so it is not asked.

### Add a generation axis

1. Add a field to `GeneratorConfig` (or the provider's config subclass) with a default
   that turns it off. It becomes a CLI flag automatically.
2. Draw it from its own tail stream in `data_generator.py`.
3. Turn it on in `scripts/campaign.py`'s axis list once it is stable.

### Add a known bug

When a finding is confirmed and written up under `issues/`, guard it so campaigns keep
finding *other* bugs:

1. In `<engine>/known_bugs.py`, match the exact shape of the failing variant (not
   anything broader) and return the bug id.
2. Confirm with `run --no-known-bugs` that the guarded variant still fails, and without
   the flag that it is withheld (`known bugs withheld` in the stats).

### Add a provider

Follow the order below; each step has a check you can run before moving on.
`ganda/surrealdb/PORTING.md` maps each module of a legacy fuzzer to its slot, and
`tests/fake_engine/` shows the whole contract in one file.

| Step | Files | Check |
|---|---|---|
| 1. Connection | `engine/connection.py` (`Server`, usually a `core.docker.DockerServer` subclass, and `Session` with a `ping()` for `doctor`), `provider.py`, `engine/limits.py` (`StorageLimits.conservative()` until probed), register in `ganda/registry.py` with `has_generators = False` | `ganda <engine> doctor`, `pytest tests/test_layering.py` |
| 2. Mapping | `storage/dialect.py`, `storage/physical_design.py`, `storage/mapping/` (one mapper per data model) | `state_check.compare_states` finds the data models in agreement |
| 3. Semantics | `oracle/semantics.py`, `oracle/pivots.py`, probe script, `RULES.md` (or a `rules/` catalog) | Probe passes; most of the work is here |
| 4. Concrete syntax | `concrete_syntax/` for all four IRs, at least one syntax per data model | Offline unit tests, then a small campaign |
| 5. Finish | `known_bugs/guards.py`, `mutation/mutants.py`, `oracle/rewrites.py`, then `has_generators = True` | `pytest` (the full slot check), campaign, mutation benchmark |

### Triage a finding

A bundle lands in `bugs/<tag>-<seed>-<generator>-<shape>/`:

| File | Contents |
|---|---|
| `README.md` | What diverged, on one screen |
| `queries.json` | Every variant's text, the bind variables, rows, mismatches |
| `<data model>_setup.json` | The exact setup plan per data model |
| `instance.json` | The abstract Instance |
| `repro.py` | Regenerates everything from (seed, config) and replays the case |

Work through it in this order:

1. **Replay it:** `python3 bugs/<bundle>/repro.py`. Known-bug guards are off during replay.
2. **Read `failing_pairs`.** A same-database pair (`graph/graph:match`) is an inconsistency
   inside one storage layout, which makes the strongest report. A cross-model pair needs
   the setup plans checked too.
3. **`internal_error`** is a raw engine exception and is a finding by itself.
4. **Reduce it by hand** to a few statements against a fresh database, then write it up
   under `issues/<id>-<slug>.md`: engine version, minimal reproduction, expected vs actual,
   analysis down to the engine source where possible, scope, and how Ganda found it.
   The existing files there show the format.
6. If the bug was Ganda's (a wrong rule, a mapping error), fix it and add a unit test
   that pins it.
