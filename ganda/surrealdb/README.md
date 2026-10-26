# SurrealDB provider

Ganda's provider for SurrealDB 3.3.4 (`surrealdb/surrealdb:v3.3.4`, the memory backend over
`/sql`). One instance is stored in three data models -- `doc` (arrays of link objects on the
source record), `graph` (relation tables and `RELATE` edges), `rel` (junction tables with
`src` / `dst` record links) -- and every question is asked of each in SurrealQL; the core's
oracle compares the answers pair by pair. The core reaches this package only through the
`Provider` interface (`core/engine/provider.py`), which `provider/provider.py` implements.

## Layout

| Folder | Holds | `Provider` member |
|---|---|---|
| `provider/` | `provider.py` (the interface's implementation), `config.py` (campaign flags: `schemafull_probability`), `physical_design.py` (index, SCHEMAFULL and write-form draws per data model) | `config_class`, `physical_design`, `semantics` (assembled here) |
| `engine/` | `connection.py` (`/sql`, Docker), `plan.py` (the setup step `Surql`), `semantics.py` (value order, equality, arithmetic), `dialect.py` (declared types, record ids, literals), `limits.py` | `server`, `limits` |
| `concrete_syntax/setup/` | the instance as a setup plan, one mapper per data model | `mappers` |
| `concrete_syntax/query/` | one renderer per IR (`traversal`, `select`, `path`, `write`), `predicate.py`, `support.py` | `*_syntax` |
| `concrete_syntax/query/predicate_sampling/` | operators, cross-type constants and leaf forms a predicate draws; `predicate_steering.py` evaluates a predicate over a record | `predicate_sampling` |
| `known_bugs/` | guards per IR (`traversal`, `select`, `write`) over `predicate.py`; `issue-drafts/` | `known_bugs` |
| `rules/` | `catalog.py` (probed rules and the defects F6, F21, F28), `checks.py`, `probe.py`, `baseline.json`, `RULES.md` | `rules`, `baseline`, `probe` |
| `mutation/` | the benchmark's mutants | `mutants` |

`engine/` imports nothing from `concrete_syntax/`. Names are imported from the module that
defines them; a package `__init__.py` re-exports only what its `Provider` member exposes.

## What SurrealQL changes

- **Records are their ids.** The logical id `n1/r0` is the record id `n1:r0` (a custom key in
  backticks); no `Ref` resolution, a plan is the same text on every replay. Rows come back with
  `id` as the logical id; every other record id in an answer stays the engine's text.
- **Two kinds of missing.** An omitted field is NONE, an explicit null NULL, both below every
  value (`<`, `<=`, `!=` hold, `=`, `>`, `>=`, `IN` fail). The core's `IS NULL` is `IS NONE OR
  IS NULL`, `IS PRESENT` is `IS NOT NONE`; a function, a cast or arithmetic over a missing
  value is refused, so those leaves are guarded by the field's presence.
- **Typed literals.** A `LET` bind variable carries no type, so a datetime, decimal or
  byte-string constant is written into the text (`d"..."`, `1.5dec`, `b"00ff"`); every other
  constant travels as `LET $name = <literal>;` before the statement.
- **The transaction door reads inside a cancelled transaction**: `BEGIN TRANSACTION; <write>;
  LET $r = <text>; CANCEL TRANSACTION; RETURN $r` -- a variable bound inside keeps its value.
- **The base class** (the inheritance axis) is the subclass tables read together: `FROM n1,
  n2, n3`, `record::tb(id) = $cls` for the polymorphic positions.
- **The array index.** An index over a LIST column keys each element; a whole-array equality
  the planner serves from it answers nothing (F28, found by this port's first campaign).

## Rules and defects

Engine facts the code depends on are records in `rules/catalog.py`, read through
`assumes(...)`, `answer(...)` and `works_around("F<n>")`. `rules/probe.py` re-checks every
record live (35 checks on 3.3.4) and writes `baseline.json`; `ganda surrealdb rules --mutate`
checks that the code depends on each. Of the legacy port's defects, F6 (a bounded shortest path)
and F21 (an index defined inside a transaction) are avoided by design; F4, F8, F19, F20, F22
and F27 no longer reproduce on 3.3.4 and are not recorded.

## Commands

```bash
pytest tests/surrealdb tests/test_layering.py -q
python3 -m ganda surrealdb doctor                 # Docker, or SURREAL_URL
python3 -m ganda surrealdb probe                  # the 35 checks; writes rules/baseline.json
python3 -m ganda surrealdb rules --write          # re-render rules/RULES.md
python3 -m ganda surrealdb rules --mutate         # does the code read every record
python3 -m ganda surrealdb run --iterations 12 --seed 1000 --workers 2 --schemafull-probability 0.5
python3 -m ganda surrealdb mutate --trials 3 --seed 1
```

A server attached through `SURREAL_URL` must run with `SURREAL_HTTP_MAX_SQL_BODY_SIZE="64 MiB"`
(the `/sql` body is capped at 1 MiB by default; the Docker server is started with it).
