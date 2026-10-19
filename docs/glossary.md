# Glossary

Ganda uses textbook terms. The legacy fuzzers it grew out of used their own; the
README's "Vocabulary" table maps them.

| Term | Meaning | Where |
|---|---|---|
| **Instance** | The abstract dataset of one seed: a labelled property graph of node classes, edge types, rows and churn. The stored state of every data model must equal it. | `core/data/model.py` |
| **Schema** | The Instance's classes, fields, indexes and edge types. Knows the provider it was generated for (`Schema.engine`). | `core/data/model.py` |
| **Churn** | Records written and then updated or deleted during setup, so the engine's write paths are exercised before any query. | `core/data/model.py` |
| **Data model** | One way an engine stores the Instance: `doc` (embedded links), `graph` (edges), `rel` (junction tables), `rdf` (triples, HANA). One database each, on one server. | `Provider.data_models` |
| **Mapping / mapper** | Turns the Instance into a setup plan for one data model. | `<engine>/mapping/` |
| **Setup plan / step** | The ordered steps that materialize the Instance. Steps are provider types, serialized into the bundle. | `core/engine/setup_plan.py` |
| **`Ref`** | A placeholder for an engine-assigned record id, resolved when the plan runs. | `core/engine/setup_plan.py` |
| **State check** | Reading every database back and comparing it with the Instance. | `core/engine/state_check.py` |
| **IR** | The abstract form of a question: traversal, select, path or write. Has an `evaluate` (or `apply`) that computes the reference. | `core/query/ir/` |
| **Query generator** | Samples IRs from its own random stream: `ir_traversal`, `ir_select`, `ir_path`, `ir_write`. The ids are stream names. | `core/query/generators/` |
| **Concrete syntax** | One way to write an IR as text in an engine's language (`match`, `flat`, `edgescan`, ...). | `<engine>/concrete_syntax/` |
| **Variant** | A key of `QueryCase.queries`: a data model (`rel`) or a data model plus a concrete syntax tag (`rel:flat`). | `core/oracle/case.py` |
| **QueryCase** | One question: variant → text, shared bind variables, comparison mode, shape. | `core/oracle/case.py` |
| **Pull-up** | A variant with the source predicate moved into an enclosing query (`<dm>:pull_up`). | traversal generator |
| **Equivalent rewrite** | A predicate rewritten into a form that must answer the same (operands reversed, arithmetic moved). | `<engine>/rewrites.py` |
| **Shape** | The structural signature of a question; the deduplication and coverage key. | `shape_signature` in each IR |
| **Oracle** | Runs every variant and compares every pair, plus the reference. | `core/oracle/oracle.py` |
| **Reference leg** | The answer computed in Python from the Instance under the provider's semantics. Off at the large scale. | `reference_for`, `IR.evaluate` |
| **Semantics** | The engine's value rules the reference reproduces: order, equality, null logic, aggregates. | `<engine>/semantics.py` |
| **Pivot** | The constant a predicate compares against, drawn on, next to or outside stored values. | `core/query/ir/predicates.py`, `<engine>/pivots.py` |
| **Finding** | A case that is not ok: `internal_error`, `error_asymmetry`, `duplicate_id_rows`, `mismatch`, `reference_mismatch`. | `CaseResult.finding_class` |
| **Failing pairs** | Which variant pairs disagreed, e.g. `graph/graph:match`. | `CaseResult.failing_pairs` |
| **Symmetric error** | Every variant rejected the question. Not a finding. | `CaseResult.symmetric_error` |
| **Finding key / deduplication** | `(generator, shape, failing pairs, class)`. Only the first finding per key is bundled. | `core/oracle/dedup.py` |
| **Bundle** | A self-contained, replayable directory for one finding. | `core/campaign/reporter.py`, `bugs/` |
| **Known bug** | A filed bug whose exact shape is withheld from campaigns and counted. | `<engine>/known_bugs.py` |
| **Probe** | Live checks of every engine fact a provider relies on. | `ganda <engine> probe`, `RULES.md` |
| **Mutant / mutation benchmark** | A deliberate corruption of the pipeline; the benchmark measures how often the oracle catches it. | `core/mutation/`, `<engine>/mutants.py` |
| **Write phase** | Predicate-targeted writes after the reads, each compared, applied to the Instance and re-checked. | `core/query/generators/write_phase.py` |
| **Transaction door** | A question read inside a transaction that writes one record and rolls back. | `SelectQuery.tx` |
| **Scale profile** | `default` or `large`: data volume, reference on or off, state sampling, timeouts. | `core/data/scale.py` |
| **Storage limits** | What an engine can hold; the large-data axes stay inside it. | `core/data/limits.py`, `<engine>/limits.py` |
| **Tail stream** | A random stream keyed by an axis's own name, so the axis off reproduces the generator without it. | `core/rng.py`, `data_generator.py` |
| **Provider / slot** | One engine behind the core; a slot is one attribute the core reads from it. | `core/engine/provider.py` |
