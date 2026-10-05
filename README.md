# Ganda

A multi-model metamorphic fuzzer for database engines, built the way SQLancer is: one
engine-agnostic **core** and one **provider** per DBMS.

One abstract property-graph `Instance` is materialized once per *data model* of an
engine (document, graph, relational) into separate databases on one server. The same
question is then asked of every data model, through several *concrete syntaxes* each,
and every pair of answers must agree, with each other and with a reference computed
from the Instance. A disagreement is a finding.

| Engine | Status |
|---|---|
| OrientDB | full provider, seed-for-seed equal to the legacy `../OrientDB` fuzzer |
| SurrealDB | skeleton: connection layer and contract, no query supply yet |
| ArangoDB | skeleton: connection layer and contract, no query supply yet |

## Running

```bash
pip install -e ".[dev]"          # or run from this directory with python3 -m ganda

ganda list                                             # the engines
pytest tests/ -q                                       # no Docker
ganda orientdb probe                                   # every engine fact the provider rests on
ganda orientdb run --iterations 100 --seed 1000 --workers 2
ganda orientdb run --iterations 20 --seed 1 --no-known-bugs      # re-confirm the filed bugs
ganda orientdb mutate --trials 8 --seed 1 --tag before-change    # the mutation benchmark
ganda orientdb run --iterations 20 --seed 1000 --coverage variant --workers 1
ganda surrealdb doctor                                 # a skeleton's connection layer
python3 scripts/campaign.py --tag c100 --iterations 100          # tests, probe, campaign, triage
python3 bugs/<bundle>/repro.py                         # replay one finding
```

`run` and `mutate` take one flag per field of the engine's configuration class
(`--max-node-types`, `--collate-probability`, ...). A server is started in Docker;
`ORIENT_URL` / `ORIENT_PASSWORD` (`ARANGO_URL`, `SURREAL_URL`) attach to a running one.

## Architecture

```
            ganda <engine> <command>
                     |
             cli.py + registry.py            engine name -> Provider (imported on demand)
                     |
   ganda.core -------------------------------------------------------------
     model, data_generator      the Instance and its seeded generation
     ast/                       predicates and the four query ASTs, with their reference
     generators/                samplers that turn an AST into cases; the write phase
     oracle, state_check        pairwise comparison, the reference leg, stored-state check
     campaign, dedup, reporter, replay, reducer
     mutation/, coverage        the mutation benchmark; the coverage hook protocol
     provider, setup_plan, docker   the contract and what providers share
                     |  Provider contract (core/provider.py)
        +------------+-------------+
   ganda.orientdb   ganda.surrealdb   ganda.arangodb
```

The core never imports a provider (`tests/test_layering.py` checks it). It reaches the
engine through the provider a schema was generated for, `Schema.engine`, or one passed
in explicitly; there is no global "current engine", so two engines can run in one
process.

### What a provider supplies

| Slot | What the core asks it |
|---|---|
| `data_models`, `config_class` | the storage models; the configuration (its fields become flags) |
| `physical_design` | index kinds and what they enforce, write forms, key rules, value pools |
| `server()` → `Server` / `Session` | start or attach; run a setup plan, execute a query text, read the stored state back |
| `mappers[data model]` | `Instance` → setup plan (the provider's own step types) |
| `semantics` | value order and equality, predicate truth, boolean logic, aggregates, null placement |
| `pivots` | which operators a predicate draws; cross-type and refined pivots |
| `traversal_syntax`, `select_syntax`, `path_syntax`, `write_syntax` | the concrete syntaxes of each AST: which apply, their text |
| `rewrites` | equivalent rewrites of a predicate |
| `known_bugs` | the filed bugs, withheld at their exact shape per variant |
| `mutants` | the engine's own corruptions for the mutation benchmark |
| `coverage()` | a collector for the engine's code coverage, or None |
| `extra_generators` | engine-only query generators |

`core/provider.py` documents every member; `tests/fake_engine/` is a complete provider
in 350 lines (an in-memory engine whose sessions answer from the AST evaluators).

### Adding a provider

1. `ganda/<engine>/connection.py`: a `Server` (subclass `core.docker.DockerServer`) and
   a `Session`. Register the provider in `registry.py`; `ganda <engine> doctor` should
   pass.
2. `dialect.py`, `physical_design.py`, `mapping/`: materialize an Instance in every
   data model and make `state_check.verify_state` agree.
3. `semantics.py`: the engine's value semantics. This is where most of the work is,
   and every rule in it should come from a live probe.
4. `concrete_syntax/`: render the four ASTs, at least one syntax per data model.
5. `known_bugs.py`, `mutants.py`, then set `has_generators = True`.

`ganda/surrealdb/PORTING.md` and `ganda/arangodb/PORTING.md` map each legacy module to
its slot.

## Vocabulary

The legacy ports used their own terms; ganda uses the conventional ones.

| Legacy | Ganda |
|---|---|
| spelling | concrete syntax (`concrete_syntax/`, `syntaxes_for`) |
| IR (`ir.py`, `select_ir.py`) | AST (`core/ast/`) |
| twin (`rw`, `rev`, `arith`) | equivalent rewrite (`rewrites.py`) |
| side | data model; an entry such as `doc:unwind` is a variant |
| materialize, `BUILDERS` | mapping, `MAPPERS` |
| guards, `case.known` | `known_bugs.py`, `case.known_bugs` |
| quarantine | deduplication (`dedup.py`) |
| minimizer | reducer |
| harness | connection (`Server`, `Session`) |
| `reference.py` | `semantics.py` |
| `orient.py` | `dialect.py` |
| family / template | query generator |
| hoist | predicate pull-up (`pull_up`) |

Kept as they were, because they are identifiers that seeds and reports depend on: the
query generator ids `ir_traversal` / `ir_select` / `ir_path` / `ir_write` (they name
random streams), the data model ids `doc` / `graph` / `rel`, per-syntax tags such as
`graph:match`, the bug ids `O1`..`O45`, and the shape strings.

## Parity with the legacy OrientDB fuzzer

`scripts/golden_dump.py` hashes, for 40 seeds under three configurations, everything
the pipeline produces without a server: the schema and instance, the three setup
plans, every read case (texts, bind variables, params, known bugs), every reference
answer, and the write phase's cases over the evolving instance. `tests/golden/
orientdb.json` is that baseline recorded from `../OrientDB`; ganda's OrientDB provider
must reproduce it.

```bash
python3 scripts/golden_dump.py --check                          # all of it (~25 s)
python3 scripts/golden_dump.py --legacy ../OrientDB --write     # re-record the baseline
python3 scripts/golden_dump.py --dump axes 7 > new.json         # one seed in full, to diff
```

A slice of the baseline runs in `pytest`. What this cannot cover is anything that
needs a server: see "Not yet verified live" in `CLAUDE.md`.

## Not ported

The LLM-written cards (`fuzzer/cards`, `llmgen/`) and the `.gr` grammar engine stay in
`../OrientDB`. DuckDB (`../duckDB`, data models `rel` / `doc` / `nst`) would be a fourth
provider; the contract's `data_models` slot is what allows it.
