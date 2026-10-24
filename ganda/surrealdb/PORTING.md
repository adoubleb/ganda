# Porting SurrealDB onto the core

What exists: `connection.py` (Docker server, `/sql` session with the `USE NS .. DB ..`
preamble), `dialect.py`, and a provider that registers. `ganda surrealdb doctor`
checks the connection layer (not yet run live).

The legacy port is `../../../surrealDB/fuzzer`, the original Ganda fuzzer. It is the
furthest from the core's design: 74 hand-written two-data-model template families, one
traversal IR, an LLM template pipeline and a rewrite algebra, with values interpolated
as literals. Porting it means re-expressing its supply as concrete syntaxes of the
core's IRs and keeping what does not fit as extra generators.

## Slot by slot

| Provider slot | Take it from (legacy) | Notes |
|---|---|---|
| `physical_design` | `model.py` (`IndexSpec`: standard / search / hnsw, `diskann_sides`, `unique`), `generator.py` (`_demote_colliding_unique_indexes`, the tail axes `_apply_diskann`, `_apply_alter_plans`, `_apply_kind_unions`) | |
| `config_class` | `generator.GeneratorConfig` | subclass the core's; the vector, view, schema-evolution and extended-type axes are SurrealDB's own |
| value types | `model.py` wrappers (`SurqlVector`, `Uuid`, `Bytes`, `Set`, `Point`, `Decimal`, `Datetime`, `Duration`) | the core's `FieldType` has no vector / uuid / bytes / geometry type: add them to the core enum (with value pools) only if a second engine wants them, otherwise keep them inside the extra generators |
| `mappers` | `materialize/{common,document,graph,relational}.py` | a setup is one SurrealQL script per data model: one `@step` class holding text is enough; `relational` is opt-in in the legacy port and a full data model here (`rel`) |
| `Session.read_nodes` / `read_edges` | new | `SELECT * FROM <table>`; record ids are `table:key`, the logical id is `table/key` |
| `semantics` | `reference.py` (`predicate_hit`: NONE sorts below every value and an absent field satisfies `<`, `<=`, `!=`; decimals in an 80-digit context; `_decimal_sum_text`; only what the generators and the comparison need) | durations have no semantics yet |
| `pivots` | `queries/templates.py`, `queries/catalogs.py` | operators are SurrealQL's (`CONTAINS`, `INSIDE`, `?=`, ...): map the IR's operator names in the renderer |
| literals | `materialize/common.surql_literal` | SurrealQL takes literals: the concrete syntax interpolates `bind_vars` through `surql_literal` rather than binding (`SurrealSession.execute` sends leftover binds as `LET`) |
| `traversal_syntax` | `ir.py` (`render_doc`, `render_graph`, `render_relational`) | one syntax per data model today; the graph arrow path, the link-array path and the `$parent` subquery |
| `select_syntax` | the node families of `queries/templates.py` | there is no select IR in the legacy port: the families that ask a filter + ids / count / aggregate / group / order become syntaxes of `SelectQuery`'s kinds |
| `path_syntax` | the recursion families (`_recursive_reference`, `_recursion_is_affordable`) | `{..+shortest=...}` paths |
| `write_syntax` | `model.WriteForm`, churn in `materialize/common.py` | the legacy port has no predicate-targeted write IR; start with `set` / `delete` |
| `rewrites` | `queries/rewrite.py` (`Rule` catalog, `derive_cases`) | its variants are `doc:r1`-style |
| `known_bugs` | `oracle._suppress_known_f4`, `templates._suppresses_known_f8`, `_suppresses_known_f19`, `_f16_signed_zero_conflict`, `_view_agg_is_comparable` (F1), `_recursion_is_affordable` (F9), `decimal_sum_is_exact` (F12 / F15), ... | collect them in one `known_bugs.py`; ids stay `F1`..`F27` (`issue-drafts/F*.md`). The legacy guards drop the case; here they mark the variant |
| `mutants` | `mutants.py` (42) | the instance-level ones overlap `core.mutation.mutants.INSTANCE_MUTANTS` |
| `coverage()` | `coverage_build.py`, `coverage_collect.py`, `coverage_runner.py` | an instrumented build from source and lcov; a `core.coverage.Collector` whose windows are profile dumps |
| `extra_generators` | the rest of `queries/templates.py` (views, events, functions, vectors, full-text), `queries/synq/`, `llm_gen/`, `template_pool.py` | each keeps its own stream name |
| `probe()` | `scripts/` (the probe and census scripts) | |

## Things that differ from OrientDB

- One namespace per seed in the legacy port; here every data model is a database of
  the namespace `ganda` (`connection.NAMESPACE`), created fresh per seed.
- A whole seed's queries went to the server in one request per data model
  (`oracle.run_cases_batched`). The core asks one case at a time; batch inside the
  session if the round trips cost too much.
- `DISTANCE_LIST` comparisons (vector search, relative tolerance 1e-6) are not a core
  `Comparison`: add it when the vector generators are ported.
- A native-binary server (`binary_path`) and the HNSW build seed
  (`SURREAL_HNSW_BUILD_SEED`) belong in `SurrealServer`.
- The legacy data model id is `relational`; ganda's is `rel`.

## Order of work

1. `doctor` live, then `mappers` and the two `read_*` methods until `compare_states`
   passes.
2. `semantics` + `pivots` + `traversal_syntax` (the three renderers already exist),
   then a campaign.
3. `select_syntax` from the node families, `known_bugs`.
4. Everything SurrealDB-only as extra generators, family by family, each checked
   against the legacy mutation reports' catch rates.

There is no parity gate against the legacy SurrealDB fuzzer: its generator draws
differently from the core's, so its seeds will not reproduce here.
