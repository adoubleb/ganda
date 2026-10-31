# OrientDB provider

Ganda's provider for OrientDB 3.2.57 (`orientdb:3.2.57`).

## Quick start: from a clone to a campaign

Needs Python 3.11 or later, and Docker (or an OrientDB server you already run).

1. Clone and install:

   ```bash
   git clone https://github.com/adoubleb/ganda.git && cd ganda
   python3 -m venv .venv && source .venv/bin/activate
   pip install -e ".[dev]"
   ```

2. Pick the server. With nothing set, Ganda starts `orientdb:3.2.57` in Docker. To fuzz
   your own build (a develop snapshot, say), copy `ganda/orientdb/.env.example` to
   `ganda/orientdb/.env` and set `ORIENT_URL` (its HTTP address, e.g.
   `http://localhost:2480`) and `ORIENT_PASSWORD` (the root password).

3. Check the setup:

   ```bash
   pytest tests/orientdb -q -n auto     # no server needed
   ganda orientdb doctor                # the server and every data model are reachable
   ganda orientdb probe                 # the engine rules Ganda relies on still hold
   ```

4. Fuzz:

   ```bash
   ganda orientdb run --iterations 100 --seed 1000 --workers 2
   python3 scripts/campaign.py --tag c100 --iterations 100            # a long campaign
   ganda orientdb run --scale large --iterations 5 --seed 1 --run-tag large   # storage-engine stress
   ```

   Other flags (`ganda orientdb run --help`): `--explain`, `--query-timeout-ms`,
   `--max-findings`, `--coverage` (JaCoCo; `ORIENT_JACOCO` for an external server).

5. Read the findings. Each is a bundle under `bugs/` (`--out-dir`), the run's statistics
   under `campaign_reports/`; `python3 bugs/<bundle>/repro.py` replays one. Read
   `failing_pairs` in its `queries.json` first: a pair within one data model
   (`graph/graph:match`) is an inconsistency with no cross-model confound.

6. Known bugs. Filed defects are withheld by default (`known_bugs/`; the list is
   `rules/catalog.py`, write-ups in `known_bugs/issue-drafts/`). To see whether your build
   fixes them, drop the guards:

   ```bash
   ganda orientdb run --iterations 20 --seed 1 --no-known-bugs
   ```

## Layout

| Folder | Holds | `Provider` member |
|---|---|---|
| `provider/` | `provider.py` (the interface's implementation), `config.py` (campaign flags; the server's link-bag thresholds `ridbag_tree_from` / `ridbag_embedded_below`), `physical_design.py` (index, storage and write-form draws per data model) | `config_class`, `physical_design`, `semantics` (assembled here) |
| `engine/` | `connection.py` (REST, Docker), `plan.py` (setup steps `Sql` / `Script` / `Batch`), `semantics.py` (value order, equality, arithmetic), `dialect.py` (declared types, index kinds, key charset), `limits.py` | `server`, `limits` |
| `concrete_syntax/setup/` | the instance as a setup plan, one mapper per data model | `mappers` |
| `concrete_syntax/query/` | one renderer per IR (`traversal`, `select`, `path`, `write`), `predicate.py`, `support.py`; `frames.py` (every concrete syntax as a frame, the data model filling its slot and the scope it takes there; the holes), `scope.py` (what an IR name is at a frame position: `Path`, `Renamed`, `Semijoin`, `Lookup`) | `*_syntax` |
| `concrete_syntax/query/predicate_sampling/` | operators, cross-type constants and leaf forms a predicate draws; `predicate_steering.py` evaluates a predicate over a record | `predicate_sampling` |
| `known_bugs/` | guards per IR (`traversal`, `select`, `write`) over `predicate.py`; `issue-drafts/` | `known_bugs` |
| `rules/` | `catalog.py` (probed rules and defects O1..O51), `checks.py`, `probe.py`, `baseline.json`, `RULES.md` | `rules`, `baseline`, `probe` |
| `mutation/` | the benchmark's mutants | `mutants` |
| `code_coverage/` | JaCoCo collector and report | `coverage` |


## Example flow

1. The core samples a schema and instance; `physical_design` gives each data model its
   own indexes, clusters and write forms; `mappers` render the setup plans and
   `engine/connection.py` runs them.
2. A core generator samples an IR question; `predicate_sampling` shapes its filter, the
   IR's renderer writes one text per cell of `frames.py` (data model and syntax).
3. `known_bugs` withholds the variants that would only re-report a filed defect.
4. Each text runs on its database; the oracle compares the answers.

## Rules and defects

Rules are for semantic alignment. For example: deleting a vertex `v` in the graph model deletes all incident edges on `v`. However, consider the equivalent process in the document model. Deleting the entry `v` deletes only the outwards edges from `v`, which is stored as an EmbeddedList field in `v`. Thus, we have to iterate over all vertex entries and their list of outgoing edges and remove any that points to `v`.

Okay, but why not just comment this fact in the translation function?

I would like to move torwards a more systematic way of generating the translation functions (which is very adhoc now). I think some attempt to give structure to the domain knowledge that produces these queries might help towards this goal.

## Common changes

- **A concrete syntax:** add it to its renderer's catalog, its cell in `frames.py`
  (frame, scope of its target, whether it is correlated), what it admits in
  `applicable`, its text in `render`, and its guard in `known_bugs/`.
- **A hole** (`frames.holes()`: a frame a data model could fill and has no cell for):
  fill it with a concrete syntax as above, then update `tests/orientdb/test_frames.py`.
- **A defect:** a probe check reproducing it, a `Defect` in the catalog, a guard with
  `works_around("O<n>")`, a write-up in `known_bugs/issue-drafts/`.
- **A rule:** a probe check, the record, and the code that reads it.