# SAP HANA provider: handoff for live development

Read this file first, then `RULES.md` (the engine facts) and the project's `CLAUDE.md`
(the rules of the core). You are continuing a provider that was built WITHOUT access to
HANA. Everything here was checked offline only; your job is to make it right against a
real SAP HANA Cloud instance.

## 1. What exists

`ganda hana ...` fuzzes four data models of ONE SAP HANA Cloud database against each
other (the oracle is purely differential: no answer is predicted in Python):

| data model | storage | query language(s) |
|---|---|---|
| `doc` | JSON Document Store: one COLLECTION per node class; relationships embedded in the source document as link arrays | collection queries (`SELECT $."f" FROM c ...`), and SQL over WITH views of the collections |
| `rel` | column tables: one per node class, one junction table per edge type, a child table per LIST column | SQL |
| `graph` | the Property Graph Engine: the rel tables again (an independent copy), one view per table under the workspace's attribute names, one GRAPH WORKSPACE `gw` over the views (`storage/mapping/graph.py`; the state read back through the engine) | openCypher inside SQL through `OPENCYPHER_TABLE` -- native texts, and rel's SQL over `g_` CTEs that read every table through the engine (`concrete_syntax/graph.py`) |
| `rdf` | Knowledge Graph Engine: one named graph; a node is an IRI, a relationship a resource of its own plus a direct link triple | SPARQL through `SYS.SPARQL_EXECUTE`, and SPARQL inside SQL through `SPARQL_TABLE` |

The graph data model was added 2026-10-11; its 32 probe facts (`G*`, `scripts/probe.py`)
ran live the same day and set the switches of `concrete_syntax/graph.py` (RULES.md "The
graph data model" has every outcome, four of them departures from the SAP docs). What is
not yet done there: a live campaign with the graph model in it, and the triage of what
it finds.

Added 2026-10-12 (RULES.md has a section each): **plan steering** -- every SQL text asked
once more under one of sixteen `WITH HINT (...)` profiles (`concrete_syntax/hints.py`:
the HEX, ESX, OLAP, join and row engines, the optimizer levels, the rewrites off, HEX's disk
operators and join kinds, no indexes, single-threaded), the variants `rel:<profile>`,
`graph:nest+<profile>`, `rdf:table_fn+<profile>`, `rel:hint_<profile>`; the Document
Store's **NOT kept as written** (`doc:neg`); the **custom-key** axis; and the
**inheritance** axis (the base class as a UNION ALL view on rel and the graph data model,
the views' union on doc, a second class triple on rdf, the unlabelled MATCH in
openCypher; a write on it per subclass). Their probe facts (`P0-P16`, `D19`, `D20`,
`D22`, `D23`, `R18`, `R19`, `K21`, `K22`, `G28`, `G29`) ran live 2026-10-12 (RULES.md).

The first ninety minutes of the 12-hour bastion campaign (`camp12h`, 2026-10-12, 25
bundles) were triaged the same day: two engine defects of the Property Graph Engine
(`findings/A8-...`: a DOUBLE constant in a cypher text is parsed to ~15 digits;
`findings/A9-...`: two OPENCYPHER_TABLE calls in one statement type the second call's
parameters in the wrong order), two quirks (B13: the triple store's AVG in double; B14:
BOOLEAN refused in COUNT and in a comparison) and three provider bugs (C11: the
reference's AVG, now `DECIMAL(25,6)` truncated; C12, C13: the native texts). The graph
renderings now bind doubles only and inline every other pivot; the `literal` variant is
gone. Live after the fixes (2026-10-12, through the Mac's tunnel): seeds 7 and 6 with
`--inheritance-probability 1 --custom-key-probability 1` (14 selects, 4 traversals, 2
paths, 4 writes each; the base class drawn in selects and writes) ran 32 and 30 cases with 0
findings, 0 setup asymmetries, 0 state leaks and 0 write-state diffs; the 125-fact probe's
new facts hold.

Later on 2026-10-12, after a review of where the oracle was blind (**track 1** of the
improvement plan): a traversal the doc views cannot ask (a LIST membership next to the
UNNEST edge views) is a PARTIAL case asked of rel, graph and rdf instead of being dropped
on every data model (`Provider.partial_traversals`, HANA on); the oracle compares the
variants that answered when another one errored, so an error asymmetry no longer hides a
wrong answer (`Provider.compare_despite_errors`, HANA on; the finding key then names the
mismatching pairs and the error's `[code]`, `Provider.error_signature`); a failure in
result decoding is an internal `EngineError` `[decode] ...` (a finding, never a dead
worker), and a message confessing an internal failure is internal under any code;
cross-type numeric pivots (`pivots.cross_type`: a double against an INT column, an
integer against a FLOAT column, below 10^12) and `NOT IN` are drawn, each typed per data
model (RULES.md "Values"); no variant repeats another's text on its data model
(`select._rewrite_changes_text`, `doc:view` on a views-only question, `rdf:from` on a
median, `rdf:native` on a null test); the rdf hint rides `table_fn` as a single-axis
variant on every question; the mutant catalog has a node-value corruption per data model
and its strict-to-inclusive corruption meets the collection filter's casts. Sixteen probe
facts ran live for it and for track 2 (R21-R24, D24-D27, K24-K27, G35-G37, P20: 141
facts in all); K26 turned up a defect of the triple store (a FILTER over COALESCE of an
OPTIONAL variable keeps or drops the wrong solutions; A10, confirmed live 2026-10-13) and
K25 closed the rdf `avg` question (the store's `xsd:decimal` arithmetic is double: B13
stays). The campaign statistics count `cases_partial`.

**Track 2** (the same day, the renderers; a live campaign with them is the next step):
every SQL question is also asked as `rel:again` (the base text a second time, through
the plan cache) and under the profile `plan_cache` (`IGNORE_PLAN_CACHE`); the collection
query itself is hinted where the store asks the question (`doc:<profile>`, D27);
`rel:inline` writes every bound value as a SQL constant (`connection.inline_binds`, R22);
`rel:block` / `rel:block_ni` run the statement inside a SQLScript anonymous block, the
binds typed in its header, through a table variable, with statement inlining on or off
(`connection.sqlscript_block`, P20); the form `tlp` asks an `ids` / `count` question as
every record EXCEPT the false ones EXCEPT the unknown ones on rel, the doc views and the
graph views, and as MINUS with `BIND ... FILTER (?t)` in SPARQL (R23 / D25 / K26); the
transaction door's PEER CHECK asks the question on a second connection before and after
the junk and files a `[dirty read]` internal error if the two answers differ (R24 / D26 /
K27 / G37; `HanaSession.peer_check`, off offline); the path question has
`graph:script` -- GraphScript's `Shortest_Path` over a second, keys-only homogeneous
workspace `gws` with its two procedures (`mapping.graph.script_ddl`, G35), read through
a SQLScript block (compared live only: the offline stand-in skips the workspace and the
procedures and drops the variant). `connection.positional` leaves `:sv_*` alone (the
SQLScript variables); `dialect.SCHEMA_TOKEN` is the session's schema in a text.

The provider fills every slot of the core's contract (`ganda/core/engine/provider.py`):
traversal, select, path and write IRs, mappings, semantics, rewrites, a probe, mutants.
`known_bugs/guards.py` guards H4 and B13; A8 and A9 are worked around in the graph renderings.

What was verified, offline only:

- `pytest tests/hana tests/test_layering.py` passes: rendering of every plan, case and
  write over several seeds; parameter binding; result decoding; the three-valued
  semantics; every rewrite keeps the truth value of every record (exhaustive check).
- `python3 -m tests.hana.run_offline --seeds 1 20` runs whole campaigns (setup, state
  check, every read case, the write phase with its state checks and read-backs) through
  the provider's OWN session code, with SQLite standing in for rel and doc and rdflib for
  rdf (`tests/hana/offline.py`). Seeds 1-20 (about 4,700 cases, every variant of every
  data model) agreed pairwise and with the reference leg of the time (since removed).
  This checks the LOGIC of every
  rendering, not HANA's syntax or semantics.
- The whole project suite passes (`pytest tests/`, 176 tests) and OrientDB's golden
  parity still holds (`scripts/golden_dump.py --check`).

What was NOT verified: anything HANA-specific. Expect syntax errors and semantic
surprises in the first live runs. That is normal; the steps below tell you how to work
through them.

## 2. Requirements on the HANA side

- SAP HANA Cloud, version 2025.2 or later, with BOTH "Document Store" and "Triple
  Store" enabled (HANA Cloud Central, Manage Configuration, Advanced Settings). The free
  tier has neither.
- A database user with: `CREATE SCHEMA`; the system privileges `SPARQL QUERY` and
  `SPARQL UPDATE` (needed even for the user's own graphs since QRC 3/2025).
- Network access from the machine to the instance's SQL endpoint (port 443).

## 3. Setup on that machine

```bash
cd ganda                                  # the project root (where pyproject.toml is)
pip install -e ".[dev,hana]"              # pytest, rdflib, hdbcli
export HANA_HOST=<instance>.hana.prod-<region>.hanacloud.ondemand.com
export HANA_PORT=443
export HANA_USER=<user>
export HANA_PASSWORD=<password>
# HANA_ENCRYPT=true and HANA_SSL_VALIDATE=true are the defaults
```

Never put the password in a file of the repository.

## 4. The order of work

Do these in order. Do not start step N+1 before step N is clean.

### Step 0: the offline baseline on that machine

```bash
pytest tests/hana tests/test_layering.py -q
python3 -m tests.hana.run_offline --seeds 1 5
```

Both must be clean before you touch anything (they were on the machine that built
this). If you change a renderer later, re-run them: they catch logic mistakes in
seconds, without HANA.

### Step 1: the connection

```bash
python3 -m ganda hana doctor
```

It creates a schema per data model, answers one query each (`SELECT 1 FROM DUMMY`, and a
SPARQL `SELECT (1 AS ?ok)`), and drops them. Problems you may meet and where to fix them
(all in `ganda/hana/engine/connection.py`):

| symptom | fix |
|---|---|
| hdbcli cannot connect / certificate errors | `connect()`: try `HANA_SSL_VALIDATE=false`; check the host and port |
| `SPARQL_EXECUTE` refused (privilege) | grant `SPARQL QUERY`, `SPARQL UPDATE` to the user |
| `SPARQL_EXECUTE` called wrongly | `HanaSession.sparql()`: the `callproc` arguments and `result[2]`. Inspect `SELECT * FROM SYS.PROCEDURE_PARAMETERS WHERE SCHEMA_NAME = 'SYS' AND PROCEDURE_NAME = 'SPARQL_EXECUTE'` |
| the JSON response cannot be parsed | `sparql_rows()`: print the raw response; check the `Accept` header in `SPARQL_QUERY_HEADERS` (CRLF between headers) |

### Step 2: the probe

```bash
python3 -m ganda hana probe          # every fact
python3 -m ganda hana probe K5 D     # some facts (an id, or a prefix R / D / K)
```

Each line is `ok`, `FAIL` (a REQUIRED fact does not hold) or `obs` (recorded only), with
what HANA answered. The probe runs in its own scratch schema and graph.

**When a probe fact fails**, the provider must change so that its renderings and its
semantics agree with what HANA really does. Do NOT edit the probe to make it pass (unless
the probe's own SQL is wrong for a reason unrelated to the fact, e.g. a typo). The table
says where each fact lives:

| facts | what they test | where to change |
|---|---|---|
| R0-R2, R15-R17 | column table DDL, types, DML row counts, error codes | `storage/mapping/relational.py`, `engine/connection.py` (`decode_row`, `_error`, `INTERNAL_CODES`) |
| R3, R4, R10 | string order, empty string, LIKE | `oracle/semantics.py` (`hana_key`, `like_hit`); the value pools in `storage/physical_design.py` |
| R5, R6 | NULL ordering, LIMIT / OFFSET | `semantics.nulls_first` / `order_key`; `concrete_syntax/select.py` (`_sql` page / keys) |
| R7-R9 | AVG, SUM, MEDIAN | `semantics.mean`, `sum_is_exact`; `connection.number` decoding |
| R11-R14 | UPSERT, MERGE INTO, MERGE DELTA, INVERTED HASH, partitions | `storage/mapping/relational.py` (and drop a write form in `storage/dialect.py` if HANA lacks it) |
| D0-D3 | collection DDL, INSERT forms, what a projection returns | `storage/mapping/document.py`; `concrete_syntax/document.py` (`typed`, `sql_cast`, `boolean_of`); `connection.decode_column` |
| D4-D7, D11, D18 | the collection filter: casts, booleans, nulls, arrays, LIKE | `concrete_syntax/document.py` (`_param`, `doc_pred`) |
| D8 | FOR ANY over an absent array | `semantics.truth` (CONTAINS on a null list) only if every data model can follow; otherwise make `doc_pred` refuse the shape (raise `Unsupported`) |
| D9, D10, D16 | UNNEST, views | `concrete_syntax/document.py` (`view_definition`) |
| D12 | collection ORDER BY | `concrete_syntax/select.py` (`_collection` page / keys) |
| D13-D15, D17 | UPDATE / DELETE forms, transactions | `concrete_syntax/write.py` (`_doc`), `concrete_syntax/tx.py` (`_doc`), `storage/mapping/document.py` |
| K0-K4, K12-K13 | SPARQL_EXECUTE, datatypes, FROM vs GRAPH, SPARQL_TABLE | `storage/dialect.py` (`sparql_literal`, `from_rdf_term`), `engine/connection.py`; if `FROM <g>` or `SPARQL_TABLE` is unusable, drop the `from` / `table` / `table_fn` variants in `concrete_syntax/traversal.py` and `select.py` |
| K5, K6 | SPARQL error propagation | if they fail, remove the `native` variants (`traversal.SYNTAXES["rdf"]`, `select._POSITIONS_OF["rdf"]`), or file it as a finding: it is HANA departing from SPARQL 1.1 |
| K7-K9, K16 | aggregates | `concrete_syntax/traversal.py` and `select.py` (`_sparql`): the `IF(COUNT(?w) > 0, ...)` guards, `HAVING` |
| K10, K11, K14, K15, K19 | paths, MINUS, updates, transactions, EXISTS in updates | `concrete_syntax/path.py`, `traversal.py`, `write.py` (`_rdf`), `storage/mapping/rdf.py` |
| K17, K18 | ordering, string functions | `oracle/semantics.py`, `concrete_syntax/methods.py` |
| P0-P17 | the hint clause; every plan-steering profile over every statement shape | `concrete_syntax/hints.py` (`PROFILES`: drop a profile a shape refuses; never a workaround in a renderer) |
| D19, D20 | NOT kept as written in a collection filter, NOT over FOR ANY | `concrete_syntax/document.py` (`doc_pred`'s `keep_not`); if one fails, drop `neg` from `select._POSITIONS_OF["doc"]` |
| R20, K23 | AVG over BIGINT as DECIMAL(25,6) (truncated to six places); the triple store's AVG in double | `semantics.mean`; `known_bugs._b13` |
| G30-G34 | integer and string constants exact, a double constant lossy (A8), two calls' parameter typing (A9), BOOLEAN in COUNT / compared refused | `concrete_syntax/graph.py` (`literal`, `cypher_filter`), `select._cypher_asks` |
| R18, D22, K21, G28 | custom keys on the four data models, their order | `dialect.is_valid_key`, `record_iri`; `semantics.hana_key`; put the axis back into `config.UNSUPPORTED_AXES` if a style fails everywhere |
| R19, D23, K22, G29 | the base class: the UNION ALL view, the views' union, the second class triple, the unlabelled MATCH | `storage/mapping/relational.py` (`base_view_ddl`), `concrete_syntax/document.py` (`view_definition`, `with_views`), `storage/mapping/rdf.py` (`class_triples`), `concrete_syntax/graph.py` (`vertex`), `concrete_syntax/write.py` (`_targets`) |

After a change: step 0 again (offline), then the probe again.

### Step 3: the first live campaign

```bash
python3 -m ganda hana run --iterations 1 --seed 1 --workers 1 --select-cases 30 --traversal-cases 10 --write-cases 3
```

Start small (the defaults issue thousands of queries per seed, each a network round
trip). Read every finding (`bugs/<tag>-<seed>-.../README.md` and `queries.json`) and sort
it into one of three kinds:

1. **A provider bug** (the common case at first): one variant fails with a SYNTAX error
   (257) or a feature-not-supported error (7), or every variant of a data model disagrees
   with the other data models in the same way. Fix the renderer or the semantics. Typical
   causes: a HANA dialect detail (a function name, a cast, a clause order), a value
   decoded with the wrong type, a semantics rule that is not HANA's.
2. **A known limitation** of a store: the store cannot ask a shape at all (documented).
   Make the shape inapplicable for that data model (`applicable()` in the syntax
   module), so the case becomes `partial` there rather than an error.
3. **A candidate engine bug**: two variants that both run, on the same or different data
   models, answer differently, and the other variants agree with one of them; or an internal
   error (`internal_error`: codes 2, 3, 129, 2048). Reproduce it by hand in a SQL console
   with the bundle's data. Reduce it to the smallest statement. Check the documentation:
   is it documented behaviour? If it is a real defect, write it up under `issues/` (see
   the existing `issues/O46-...md` for the format) with an id `H1`, `H2`, ..., and add
   a guard in `known_bugs/guards.py` that withholds exactly that shape (the OrientDB provider's
   `known_bugs/guards.py` is the pattern: a function of the IR and the concrete syntax).

Grow the campaign only when a seed runs clean: `--iterations 5`, then the defaults, then
`--workers 2` (each worker has its own schemas and graphs).

### Step 4: record what holds

When a fact is confirmed live, change its status in `RULES.md` from `assumed` to
`probed <date> <HANA version>`. When the provider changes because HANA behaves
differently from what is written here, update `RULES.md` and the docstring of the
module you changed. Keep `CLAUDE.md`'s HANA section current.

## 5. Map of the package

```
ganda/hana/
  provider.py          the slots; `configure` refuses the core axes not modelled yet
  config.py            HanaConfig (id index axis, partition axis), UNSUPPORTED_AXES
  engine/
    connection.py      HanaServer / HanaSession (hdbcli), parameter binding (`positional`, `inline_binds`, `sqlscript_block`), decoding (a failure is a `[decode]` finding), the transaction door's peer check
    limits.py          what HANA can hold (documented or chosen; nothing probed)
  storage/
    dialect.py         SQL types, LIST storage, the triple store's IRIs and literals, write forms
    physical_design.py index kinds, id index and partition draws, write forms, value pools
    mapping/           one setup plan per data model (document.py, relational.py, rdf.py)
  oracle/
    semantics.py       value semantics: three-valued logic, order, exact sums
    pivots.py          operators a predicate draws (null tests: IS NULL / IS NOT NULL only)
    rewrites.py        equivalent predicate trees (three-valued), arithmetic rewrite
  concrete_syntax/
    predicate.py       a predicate as SQL (rel, doc views) and as a SPARQL filter
    document.py        the doc data model: collection filter (NOT pushed down or kept), views, typed reads
    graph.py           the graph data model: the `g_` views family and the native openCypher family
    hints.py           plan steering: the `WITH HINT (...)` profiles every SQL text is asked under once more
    traversal.py       the traversal IR: rel join / scalar, doc over views, graph match / paths / undirected, rdf graph / from / link / minus / table / native, every data model's hint_<profile>
    select.py          the select IR: filter x position x form x hint
    path.py            the path IR: levels of walks
    write.py           the write IR and its execution steps (a base-class write per subclass)
    tx.py              the transaction door of the select IR
    methods.py         the method catalog (SQL / SPARQL / Python mirror)
  known_bugs/guards.py the guards: H4 (a doc traversal sum the views overflow), B13 (an rdf avg where double arithmetic parts from DECIMAL(25,6))
  mutation/mutants.py  the mutation benchmark's catalog
  scripts/probe.py     `ganda hana probe`
tests/hana/
  test_units.py        no server, no rdflib
  test_offline.py      the offline campaigns (needs rdflib)
  offline.py           the SQLite / rdflib stand-in (its translations and their limits)
  run_offline.py       `python3 -m tests.hana.run_offline --seeds A B`
```

## 6. Conventions you must keep

- The core (`ganda/core`) imports no provider, and engine behaviour never goes into the
  core (`CLAUDE.md`, "Rules of the core"). If HANA needs something the core cannot
  express, add a provider hook the way `CLAUDE.md` describes, not an `if engine == ...`.
- `ganda/hana` imports no other provider (`tests/test_layering.py`).
- Setup steps are registered in a registry every provider shares: HANA's op names are
  prefixed `hana_` (`storage/mapping/common.py`); the core refuses an op name another class
  already holds.
- Variant names are `<data model>:<tag>` (`rel:scan`, `rdf:jscan+table_fn`); the tags
  are the syntax module's choices. A new syntax is a new choice in its catalog, what it
  admits in `applicable`, its text in `render`, and a line in the module docstring.
- Use conventional terms (the README glossary).
- Every engine fact the provider starts to rely on gets a probe fact and a line in
  `RULES.md`.

## 7. Known gaps and ideas, in order of value

1. Nothing live yet: steps 1-4.
2. `doc` read-back: `HanaSession.read_nodes` reads `SELECT * FROM c` and parses the
   JSON text. Check that HANA serializes a DECIMAL as a JSON number that parses back to
   the same double (probe D1).
3. Collections RETURNING JSON (the type SAP recommends) are a second document-store
   engine path: views over them follow JSON semantics and let filters push down. A
   physical-design axis (per seed: NVARCHAR or JSON collections) would compare the two.
   It needs the `$.` paths (already used) and a different reading of projected values
   (JSON text). Probe it first.
4. `NOT (...)` in a collection filter: the doc filter pushes NOT down because the
   documented grammar does not show NOT. If HANA accepts `NOT (...)` and `NOT FOR ANY`,
   add a doc variant that keeps the NOT.
5. Narrow types (INTEGER, REAL, DECIMAL), dates, maps, binaries, collation: refused by
   `config.UNSUPPORTED_AXES`. Each needs its value forms in all four data models and its
   semantics (dates and maps share one core axis, `date_map_probability`; a map has no
   HANA type, so it would be JSON text plus a child table of entries on rel, an object on
   doc, entry triples on rdf). Inheritance and custom keys are modelled (2026-10-12).
6. The graph data model (done 2026-10-11, offline): what is left there -- GraphScript
   (`SHORTEST_PATH`, `NEIGHBORS` in a `DO ... LANGUAGE GRAPH` block) as path and reach
   syntaxes; a workspace on a JSON COLLECTION (`CREATE GRAPH WORKSPACE ... COLLECTION
   ... TYPE / VERTEX KEY / EDGE KEY SOURCE TARGET`) as a second backing drawn per seed;
   `RDF_GRAPH_TO_GRAPH_WORKSPACE` loading the graph from the rdf model (new in QRC
   2/2026); typed parameters (`graph.TYPED_PARAMS`) as a syntax of their own.
7. Plan steering (done 2026-10-12, `concrete_syntax/hints.py`): what is left -- a hint
   on the compared statement of a WRITE (`UPDATE ... WITH HINT`), and the column-view
   estimation hints once a shape is found that they change. A base-class question on
   doc as a UNION ALL of collection queries is out: the Document Store refuses it (D23).
8. Track 2 of the improvement plan (probed and rendered 2026-10-12, its first live
   campaign still to run): what is left of it -- a reach traversal through GraphScript's
   `Neighbors` (G35: the four-argument form, outgoing; an edge-induced `SubGraph` drops
   a vertex with no edge of the KIND, so a per-start guard is needed inside the block,
   or a GraphScript loop over the starts: probe it first, G38); the `block` positions on
   the doc views and the graph views (rel only so far); a hint on a write's statement.
   Also to file: **A10** (K26; `findings/A10-sparql-filter-coalesce-optional-variable/repro.py`,
   run live 2026-10-13; it still needs a README), and the GraphScript doc check for G35's verdict.

## 8. The offline stand-in's limits

`tests/hana/offline.py` translates HANA's SQL into SQLite and runs SPARQL on rdflib. It
cannot run: `MEDIAN` in SPARQL; the `native` variants (rdflib propagates errors through
`&&` wrongly); the rdf `jscan` variants with an `EXISTS` (an rdflib join bug).
`run_offline.py` leaves those variants out; `test_offline.py::test_rdflib_quirks` pins
the rdflib behaviours, so you will notice if rdflib fixes them. SQLite has no booleans
(the stand-in stores them as two marker strings) and no MERGE / UPSERT (translated). A
disagreement offline is a renderer or semantics bug unless it is one of these.

The graph data model runs on SQLite too: its views are plain views, the workspace step
is recorded rather than run, and every `OPENCYPHER_TABLE(...)` in a text is rewritten
into a SQLite subquery by `tests/hana/opencypher_sqlite.py` -- a translator of exactly
the openCypher subset the renderers emit, mirroring the engine's documented semantics
through switches named after the probe facts (`SELF_LOOP_TWICE`,
`ISOMORPHIC_WITHIN_CLAUSE`, `TRAIL_PATHS`, `SUM_EMPTY_ZERO`, `NULLS_FIRST_ASC`). A text
it does not cover makes its case partial (`run_offline._offline`, `translatable`); a new
cypher construct in a renderer needs the translator extended, or that variant is
compared live only. An unlabelled vertex (the base class) is the union of the vertex
views over the attributes they all share.

SQLite has no hint clause: the offline session strips it (`hints.strip`), so a hinted
twin runs as its plain statement offline -- plan steering is compared live only; the
offline runs check that every hinted text is a well-formed statement and that the
variants' plumbing (one profile per question, the tags, the bundles) holds.
