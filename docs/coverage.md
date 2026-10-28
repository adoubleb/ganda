# Code coverage and metamorphic coverage

`ganda <engine> run --coverage variant --workers 1` measures what the engine's code
reaches during a campaign, window by window (one window per variant asked), and from those
windows the **metamorphic coverage** (MC) of the campaign (`ganda/core/engine/mc.py`).

## What MC is here

Ba, Jiang and Rigger ("Metamorphic Coverage", ISSTA 2026) define MC over a pair of test
inputs that a metamorphic relation holds between: the code one reaches and the other does
not. A Ganda case has N variants (one question per data model and concrete syntax), so
MC is N-way: an element is in a case's MC when SOME of its compared variants reach it and
some do not (0 < k < N). Over a connected comparison graph that equals the union of the
pairwise symmetric differences over every pair, the paper's decomposition, so the
headline is the paper's number; the per-data-model-pair breakdown is kept beside it.

The summary (`<tag>-coverage.json`, key `mc`) has:

| field | meaning |
|---|---|
| `mc` | elements in some case's MC (background taken out) |
| `covered` | elements some compared variant reached |
| `mc_of_covered`, `mc_of_elements` | the ratios (`elements`: every counter of the build, or the probes of the classes hit) |
| `zero_mc_cases` | cases whose variants all reached the same code (a weak relation, paper §4) |
| `data_model_pairs` | MC per pair of data models (`doc~graph`, ...) |
| `generators`, `modules` | MC per query generator and per package / namespace / crate |
| `background`, `background_in_mc` | what the server reached in idle windows (5 s after the first setup), left out of MC |

The background matters because the engines are servers: timers and housekeeping threads
run inside any window (an idle ArangoDB moves about 4 500 counters a second). Elements a
variant reaches only on its first run (lazy initialization) are not filtered.

## The collectors

- **OrientDB (Java)**: the JaCoCo agent (`ganda/orientdb/code_coverage/collector.py`); an
  element is a probe; each window is a reset dump over the agent's TCP port. Needs
  `JACOCO_AGENT` (an `org.jacoco.agent-0.8.13-runtime.jar`).
- **SurrealDB (Rust) and ArangoDB (C++)**: LLVM source-based coverage in CONTINUOUS mode
  (`ganda/core/engine/llvm_coverage.py`). The server writes
  `<stats dir>/<tag>-profile/<pid>.profraw` (bind-mounted at `/ganda-profile`), and the
  counters live in that file while it runs; a window is the counters that changed since
  the previous read. An element is a counter. The profile is never written by Ganda, so
  `<tag>-coverage/*.profraw` stays a cumulative profile for `llvm-profdata merge` and
  `llvm-cov report --instr-profile ... <binary>`.

## The instrumented images (mousedeer)

Sources: `/home/hakam/ganda/cov-src/{surrealdb-3.3.4,arangodb-3.12.11}` (the release tags).

- SurrealDB: `scripts/coverage/build-surrealdb.sh` in `rust:1.95-bookworm`; only the
  `surreal` / `surrealdb*` crates are instrumented (`rustc-cov-wrapper.sh` as
  `RUSTC_WRAPPER`), release without LTO. Image `ganda-cov/surrealdb:3.3.4` (`/surreal` on
  `debian:bookworm-slim` with `ca-certificates` and `SURREAL_BIND=0.0.0.0:8000`, as the
  official image: `scripts/coverage/surrealdb.Dockerfile`). 422 k counters in 140 k
  functions. `llvm-profdata` / `llvm-cov` for it are Rust's (`rustup component add
  llvm-tools`, LLVM 22; copied to `cov-src/surrealdb-build/llvm-bin` with its `libLLVM`).

- ArangoDB: `scripts/coverage/build-arangodb.sh` in ArangoDB's CI build image
  (`public.ecr.aws/b0b8h2r4/arangodb/ubuntubuildarangodb-312:24.04-f7032bef0`, clang
  19), the `pr-non-maintainer-x64` preset with `USE_ENTERPRISE=Off`, target `arangod`
  only (about 25 min at 48 jobs). The public v3.12.11 tag does not build as the community
  edition as it is: `scripts/coverage/arangodb-community-build.patch` adds a missing
  include (`ClusterHotBackup.cpp`), a community stand-in for the enterprise
  `SmartGraphProvider` test (`algorithm-aliases.h`) and the `aql::` qualifiers of the
  community stub in `HandleViews.cpp`. `-fprofile-list` (`arangod/`, `lib/`) did not keep
  the bundled V8 and abseil headers out, so the build has 1.17 M counters in 355 k
  functions (a third of them `v8::internal`; V8 alone is 803 k of the 1.67 M source lines).
  The ArangoDB collector therefore measures only the `arangodb::` namespace (`scope` of
  `LlvmCollector`; 281 k counters): unscoped, V8 made up 39.5 k of a 70 k MC (the
  JavaScript transaction door, the compiler and the GC), none of it ArangoDB's query logic.
  Image `ganda-cov/arangodb:3.12.11` (`scripts/coverage/arangodb.Dockerfile`: the
  official `arangodb:3.12.11` with the instrumented, static `arangod`, which reports
  `"license":"community","version":"3.12.11"`).

The reader was checked against `llvm-profdata show --all-functions --counts` on a live
SurrealDB profile: every one of the 61 214 functions with a unique name had the same
counters (raw version 10, 64-byte data records, the counters NOT page-aligned under
counter relocation, a negative `CountersDelta`).

```bash
SURREAL_IMAGE=ganda-cov/surrealdb:3.3.4 python -m ganda surrealdb run --coverage variant --workers 1 --iterations 12 --seed 1000 --run-tag mc-surreal
ARANGO_IMAGE=ganda-cov/arangodb:3.12.11 python -m ganda arangodb run --coverage variant --workers 1 --iterations 12 --seed 1000 --run-tag mc-arango
JACOCO_AGENT=/home/hakam/ganda/cov-src/jacocoagent.jar python -m ganda orientdb run --coverage variant --workers 1 --iterations 12 --seed 1000 --run-tag mc-orient
```

The three element kinds (JaCoCo probes, LLVM counters of two languages) are not the same
unit, so compare MC across engines as ratios (`mc_of_covered`), not counts.

## Pilot (2026-10-26, mousedeer, seeds 1000-1002, `--coverage variant --workers 1`)

| | OrientDB 3.2.57 | SurrealDB 3.3.4 | ArangoDB 3.12.11 (`arangodb::`) |
|---|---|---|---|
| element | JaCoCo probe | LLVM counter | LLVM counter |
| cases measured | 610 | 639 | 639 |
| covered | 18 223 | 9 891 | 19 005 |
| MC | 11 319 (62.1 %) | 7 352 (74.3 %) | 13 967 (73.5 %) |
| zero-MC cases | 0 | 12 | 0 |
| background taken out | 247 | 390 | 1 837 |

Line coverage of the whole campaign (`llvm-cov report`): SurrealDB 18.6 % of 197 k lines
(core 26.1 %, `core/exec` 32.5 %, syn 26.2 %, kvs-mem 67.1 %; graphql, mcp and the rocksdb /
tikv / surrealkv backends 0 %); ArangoDB `arangod/` 25.3 % of 306 k lines (`Aql` 42.3 %,
`RocksDBEngine` 35.2 %, `Graph` 26.1 %; Cluster, Agency, Replication and IResearch under 5 %).
The counter percentages are far lower (SurrealDB 3.8 %) because every monomorphized or
inlined copy of a function carries its own counters.

