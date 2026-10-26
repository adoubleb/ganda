# OrientDB provider

Ganda's provider for OrientDB 3.2.57 (`orientdb:3.2.57`). One instance is stored in three
data models -- `doc` (embedded link lists), `graph` (vertices and edges), `rel` (junction
classes) -- and every question is asked of each in OrientDB SQL; the core's oracle compares
the answers pair by pair. The core reaches this package only through the `Provider` interface
(`core/engine/provider.py`), which `provider/provider.py` implements.

## Layout

| Folder | Holds | `Provider` member |
|---|---|---|
| `provider/` | `provider.py` (the interface's implementation), `config.py` (campaign flags), `physical_design.py` (index, storage and write-form draws per data model) | `config_class`, `physical_design`, `semantics` (assembled here) |
| `engine/` | `connection.py` (REST, Docker), `plan.py` (setup steps `Sql` / `Script` / `Batch`), `semantics.py` (value order, equality, arithmetic), `dialect.py` (declared types, index kinds, key charset), `limits.py` | `server`, `limits` |
| `concrete_syntax/setup/` | the instance as a setup plan, one mapper per data model | `mappers` |
| `concrete_syntax/query/` | one renderer per IR (`traversal`, `select`, `path`, `write`), `predicate.py`, `support.py` | `*_syntax` |
| `concrete_syntax/query/predicate_sampling/` | operators, cross-type constants and leaf forms a predicate draws; `predicate_steering.py` evaluates a predicate over a record | `predicate_sampling` |
| `known_bugs/` | guards per IR (`traversal`, `select`, `write`) over `predicate.py`; `issue-drafts/` | `known_bugs` |
| `rules/` | `catalog.py` (probed rules and defects O1..O46), `checks.py`, `probe.py`, `baseline.json`, `RULES.md` | `rules`, `baseline`, `probe` |
| `mutation/` | the benchmark's mutants | `mutants` |
| `code_coverage/` | JaCoCo collector and report | `coverage` |

`engine/` imports nothing from `concrete_syntax/`. Names are imported from the module
that defines them; a package `__init__.py` re-exports only what its `Provider` member exposes
(`provider/`, `known_bugs/`, `predicate_sampling/`).

## A case, end to end

1. The core draws a schema and instance; `physical_design` gives each data model its
   own indexes, clusters and write forms; `mappers` render the setup plans and
   `engine/connection.py` runs them.
2. A core generator draws an IR question; `predicate_sampling` shapes its filter, the
   IR's renderer writes one text per data model and syntax.
3. `known_bugs` withholds the variants that would only re-report a filed defect.
4. Each text runs on its database; the oracle compares the answers.

## Rules and defects

Engine facts the code depends on are records in `rules/catalog.py`, read through
`assumes(...)`, `answer(...)` and `works_around("O<n>")`. Keep a rule only if a
different answer would change how a question is made equivalent across data models
or how setup works. `rules/probe.py` re-checks every record live and writes
`baseline.json`; `ganda orientdb rules --mutate` checks that the code depends on each.

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
