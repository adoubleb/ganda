# OrientDB provider

Ganda's provider for OrientDB 3.2.57 (`orientdb:3.2.57`).

## Layout

| Folder | Holds | `Provider` member |
|---|---|---|
| `provider/` | `provider.py` (the interface's implementation), `config.py` (campaign flags), `physical_design.py` (index, storage and write-form draws per data model) | `config_class`, `physical_design`, `semantics` (assembled here) |
| `engine/` | `connection.py` (REST, Docker), `plan.py` (setup steps `Sql` / `Script` / `Batch`), `semantics.py` (value order, equality, arithmetic), `dialect.py` (declared types, index kinds, key charset), `limits.py` | `server`, `limits` |
| `concrete_syntax/setup/` | the instance as a setup plan, one mapper per data model | `mappers` |
| `concrete_syntax/query/` | one renderer per IR (`traversal`, `select`, `path`, `write`), `predicate.py`, `support.py`; `frames.py` (every concrete syntax as a frame, the data model filling its slot and the scope it takes there; the holes), `scope.py` (what an IR name is at a frame position: `Path`, `Renamed`, `Semijoin`, `Lookup`) | `*_syntax` |
| `concrete_syntax/query/predicate_sampling/` | operators, cross-type constants and leaf forms a predicate draws; `predicate_steering.py` evaluates a predicate over a record | `predicate_sampling` |
| `known_bugs/` | guards per IR (`traversal`, `select`, `write`) over `predicate.py`; `issue-drafts/` | `known_bugs` |
| `rules/` | `catalog.py` (probed rules and defects O1..O46), `checks.py`, `probe.py`, `baseline.json`, `RULES.md` | `rules`, `baseline`, `probe` |
| `mutation/` | the benchmark's mutants | `mutants` |
| `code_coverage/` | JaCoCo collector and report | `coverage` |


## Example flow

1. The core samples a schema and instance; `physical_design` gives each data model its
   own indexes, clusters and write forms; `mappers` render the setup plans and
   `engine/connection.py` runs them.
2. A core generator samples an IR question; `predicate_sampling` shapes its filter, the
   IR's renderer writes one text per data model and syntax.
3. `known_bugs` withholds the variants that would only re-report a filed defect.
4. Each text runs on its database; the oracle compares the answers.

## Rules and defects

Rules are for semantic alignment. For example: deleting a vertex `v` in the graph model deletes all incident edges on `v`. However, consider the equivalent process in the document model. Deleting the entry `v` deletes only the outwards edges from `v`, which is stored as an EmbeddedList field in `v`. Thus, we have to iterate over all vertex entries and their list of outgoing edges and remove any that points to `v`.

Okay, but why not just comment this fact in the translation function?

I would like to move torwards a more systematic way of generating the translation functions (which is very adhoc now). I think some attempt to give structure to the domain knowledge that produces these queries might help towards this goal.

## Common changes

- **A concrete syntax:** add it to its renderer's catalog, what it admits in
  `applicable`, its text in `render`, and its guard in `known_bugs/`.
- **A defect:** a probe check reproducing it, a `Defect` in the catalog, a guard with
  `works_around("O<n>")`, a write-up in `known_bugs/issue-drafts/`.
- **A rule:** a probe check, the record, and the code that reads it.

## Commands

```bash
pytest tests/orientdb tests/test_layering.py -q
python3 -m ganda orientdb probe                  # Docker, or ORIENT_URL / ORIENT_PASSWORD
python3 -m ganda orientdb rules --write          # re-render rules/RULES.md
python3 -m ganda orientdb rules --mutate         # does the code read every record
python3 -m ganda orientdb run --iterations 20 --seed 1000 --workers 2
```
