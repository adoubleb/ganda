# Porting ArangoDB onto the core

What exists: `connection.py` (Docker server, AQL cursor session with bind variables
and per-variant cursor options, `explain`), `dialect.py`, and a provider that
registers. `ganda arangodb doctor` checks the connection layer (not yet run live).

The legacy port is `../../../arangoDB/fuzzer`. It already has the four ASTs and their
concrete-syntax catalogs (it took the approach from the OrientDB port), so this is the
cheaper of the two remaining engines.

## Slot by slot

| Provider slot | Take it from (legacy) | Notes |
|---|---|---|
| `physical_design` | `model.py` (`INDEX_KINDS`, `IndexSpec`), `generator.py` (index-option and topology draws, `_demote_colliding_unique_indexes`, `_demote_unsafe_mdi_indexes`) | index kinds are `persistent` / `mdi` / `inverted` / `mdi-prefixed`; keep each axis on its own tail stream |
| `config_class` | `generator.GeneratorConfig` | subclass the core's; add only the fields the core lacks (search, geo, computed values, named graph, topology) |
| `mappers` | `materialize/{common,document,graph,relational}.py` | the steps (`CreateCollection`, `CreateIndex`, `InsertDocuments`, `Aql`, `Transaction`, ...) become `@step` classes; `Session.apply_setup` dispatches on them as `harness.apply_setup` does |
| `Session.read_nodes` / `read_edges` | new (the legacy tests used an in-memory write model) | needed by `state_check.verify_state` |
| `semantics` | `reference.py` (`compare_across_types`, the exact-sum gate below 2^53, ICU collation: no reference for ordered string predicates, two-valued logic) | AQL orders across types (`dialect.TYPE_ORDER`); an absent attribute reads as null |
| `pivots` | `queries/ir_generator.py`, `templates.py` (how predicates are drawn) | AQL has no `IS DEFINED`; null tests are `== null` |
| `traversal_syntax` | `queries/ir.py` (`render`), `queries/spellings.py` | `graph:escan`, `doc:join`, `relational:key`, `flat`, `hoist` (here: `pull_up`) |
| `select_syntax` | `queries/select_ir.py`, `queries/select_spellings.py` | filter x position x form, as in OrientDB |
| `path_syntax` | `queries/path_ir.py` | |
| `write_syntax` | `queries/write_ir.py`, `queries/dml.py` | writes run in a stream transaction that is always aborted: `execute` opens and aborts it |
| `rewrites` | `queries/rewrites.py` (26 structural rewrites, `complement_is_a15`) | variants `<data model>:rw_<tag>` |
| `known_bugs` | the scattered `_a<N>_shape` functions (`templates`, `expressions`, `geo`, `ir_generator.a22_shape`, `select_spellings.a35_shape`, `write_ir.a21_shape`, ...) | collect them in one `known_bugs.py`; ids stay `A1`..`A35` (`issue-drafts/A*.md`) |
| `mutants` | `mutants.py` | its instance-level mutants are already `core.mutation.mutants.INSTANCE_MUTANTS`; add the rest |
| `extra_generators` | `queries/{expressions,model_fns,search,relsearch,numsearch,geo}.py`, the hand-written families of `templates.py` | the engine-only supply: ArangoSearch, geo, expression shapes. Each gets its own stream name and a `reference_for` branch |
| plan variants | `plan_steer.py`, `templates.add_plan_variants` | a variant's optimizer rules go in `QueryCase.variant_options`; `ArangoSession.execute` already passes them to the cursor |
| `probe()` | `scripts/probe_aql.py` | |

## Things that differ from OrientDB

- The legacy data model id is `relational`; ganda's is `rel`.
- Error 1655 (an expired transaction) is transient, never a finding: handle it in the
  session (retry) rather than in the oracle.
- `failOnWarning` is on for every query (`connection.QUERY_OPTIONS`).
- A 5xx the server answered is an internal error (`EngineError(internal=True)`), not a
  crash; the connection layer already does this.
- The latest index and search axes need the Enterprise image (`ARANGO_IMAGE`).

## Order of work

1. `doctor` live, then `physical_design` + `mappers` + the two `read_*` methods until
   `verify_state` passes on generated instances.
2. `semantics` + `pivots`, then `traversal_syntax` and `select_syntax` with one syntax
   per data model; run a campaign; add the remaining syntaxes.
3. `known_bugs` (a campaign with `--no-known-bugs` should re-find A1..A35).
4. `write_syntax`, `path_syntax`, `rewrites`, plan variants, `mutants`.
5. The extra generators.

There is no parity gate against the legacy ArangoDB fuzzer: it draws its instances
differently from the core's generator, so its seeds will not reproduce here.
