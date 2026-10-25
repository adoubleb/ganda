# Refactoring a provider to the multi-model scope

What was done to the OrientDB provider between `a31c235` and `305a2dc` (2026-10-23 to
2026-10-25), written as the procedure to repeat for ArangoDB and HANA. The steps are in
the order to apply them, which is not quite the order OrientDB went through: two of its
phases had to be partly undone once campaigns showed what they broke (see *Lessons*).

## The scope this enforces

Ganda tests one claim: **the same question, asked of the same data in different data
models of one engine, gets the same answer.** Everything below follows from it:

- Two variants are compared only if their **data models differ** (and, while IR-level
  spellings exist, their spelling is the same). Never two syntaxes of one data model,
  never one data model's index path against its own scan.
- Each data model keeps its **own physical design**: an index on one data model and not
  another is fine.
- An engine fact matters only if getting it wrong could make **two data models differ**,
  or make a statement invalid. A fact that holds alike on every data model cannot cause a
  false finding and is not recorded.
- No data model's answer or state may be **computed by Ganda** (the Instance) instead of
  by the engine: that would compare the engine against Ganda's model of it.

## Step 1. Engine rules as records

**What.** Move every engine fact the provider relies on out of prose (`RULES.md`, comments,
probe strings) into `ganda/<engine>/rules/catalog.py`:

- `Rule(question, subject, answer)`: the question has a kind (COMPARE, ASK, EQUIVALENT,
  EXACT); the subject is the engine path it holds on (OrientDB: `osql`, `osql@index`,
  `match`, `traverse`, `script`, `batch`).
- `Defect(id, subject)` for every engine bug a guard withholds or the design avoids.
- `ErrorClass` for the engine's error families the connection classifies (OrientDB: the
  timeout).

`RULES.md` is generated from the catalog (`ganda <engine> rules --write`), never edited.

**Readers** (`ganda.<engine>.rules`), so the code names the fact where it decides on it:

| reader | use where the code |
|---|---|
| `answer(q, subject)` | branches on the value (a limit, an operator list) |
| `refused(construct, subject)` / `alike(q, a, b)` | branches on acceptance / agreement |
| `assumes(q, subject, expected)` | has no branch for another answer (an import-time assertion) |
| `works_around(defect)` | avoids a defect by design instead of guarding it |

A static scan (`read_index()`) finds every reader, so `RULES.md` lists who reads each rule.

## Step 2. Every record read, every check linked, a baseline instead of a lock

- `rules/checks.py` maps each probe check (`C<n>`, never reused) to the rules it asserts,
  and `REPRODUCED_BY` maps each defect to the checks whose expected answer is the
  defect's. **Every check is listed against something**; a check of nothing is deleted.
- A rule nobody reads is deleted. A defect nobody guards or avoids is retired.
- `rules/baseline.json` replaces the lock: the probe writes it when every rule's checks
  hold, recording the server's build, the checks that held and the defects **fixed** there
  (a defect that stops reproducing is a bug fixed: recorded, not a probe failure).
- `ganda <engine> run` **warns** at startup when the server is another build than the
  baseline's (`Server.version()`, the provider's `baseline` slot); it does not refuse.
- Pin the provider's default image to the baseline's build.

## Step 3. The catalog mutation check

`ganda <engine> rules --mutate` (`ganda/core/rules/mutation.py`): each rule given another
answer, each defect removed, in a fresh interpreter; a mutant is caught when an import
fails (`assumes`, `works_around`) or the generated setup plans, cases and `engine.limits`
of 20 seeds change. **A survivor is a record nothing decides on**: delete it or make the
code read it. Run it after every catalog change. Caveat: catching an `assumes` mutant
proves the line exists, not that behaviour depends on it; that judgement is the probe
review's (step 6).

## Step 4. Compare data models only

**4a. Remove within-model comparisons.** Delete every variant that exists only to make one
data model use or avoid an index. OrientDB removed: the select positions `scan` / `nest`,
the `index:` door, `jscan`, the semi-join's subquery-target rewrite. **HANA still has:**
the sixteen `WITH HINT` plan-steering profiles (`rel:<profile>`, `graph:nest+<profile>`,
`rdf:table_fn+<profile>`, `rel:hint_<profile>`), and the forms that ask rel against
itself (`rel:again`, the `plan_cache` profile, possibly `rel:inline`, `rel:block` /
`rel:block_ni`). Decide each: a form that is the same question on every data model may
stay as a cross-model spelling; one that exists to compare a data model with itself goes.

**4b. Pair only across data models** (core, done once for every engine, `18f7263`):
`QueryCase.spellings` records each variant's IR-level spelling, `comparable_pairs`
keeps pairs whose data models differ and whose spellings are equal, error asymmetries and
"asserts anything" are judged per compared pair, and a variant with no partner is not run.
The provider only has to record `spellings` correctly (`select.py`: `sp.tag`; traversal:
`pull_up`).

## Step 5. Rewrites become drawn predicates

The rewrites (`rw`, `rev`, `arith`, `literal`) existed to compare a filter with an
equivalent spelling of itself. Under step 4 they are compared only across data models, so
they need not be equivalent, and need not be derived at all.

**5a. Remove the equivalence machinery** (`31872de`): the `IS NOT NULL` / `OR IS NULL`
wrappers, the refusals that kept a rewrite exact (signed zeros, case-insensitive `IN` /
`BETWEEN`, inexact arithmetic), `LOWER=`, and the rules read only there. Keep what keeps a
statement **valid**: every variant's parameters travel in one request, so a pivot the
engine cannot take (a non-finite double, an integer past the long range) fails all of
them.

**5b. Draw each leaf's form** (`7ef1666`). The core predicate generator calls the engine's
optional `predicate_sampling.leaf_form(rng, leaf)` after drawing a leaf; OrientDB's draws reversed
operands, the field under arithmetic (the drawn pivot shifted alike), a literal pivot, a
parameter-keyed map entry. `rw` needs no replacement: the generator already draws its
shapes (`IN [x]`, `NOT (...)`, ranges, De Morgan) as ordinary compounds. Every IR gets the
forms (select, traversal, path, write, semi-join subqueries). Then remove:

- the select and write `filter` axes and their parts of the per-case material;
- `oracle/rewrites.py`; `arith_exact` moves to `Semantics` (a contract member), and the
  provider's `rewrites` slot goes;
- the guards' judgement of a "rewritten" predicate (they judge `q.where`), and the
  rewrite-only guards (OrientDB: O12's select-literal guard became avoidance by design).

Constraints the form draw must respect (OrientDB): no arithmetic or literal on a
relationship attribute the doc data model holds untyped; no literal a type would not
hold (O12); the **write generator** keeps an arithmetic filter only where it is exact on
every stored value of its field (`_filter_exact`), so the Instance stays usable for
steering.

**5c. Writes share one spelling.** The write's target and door are drawn once for every
data model (`write_syntax.spelling(sp)`, core `choose_syntaxes`); only each data model's
form differs.

## Step 6. Shrink the catalog by evidence

**6a. Review every probe check against the scope** (`777d927`): keep a check only if some
decision is correct only while its fact holds, and a change would compare paths that then
legitimately differ, send statements the engine refuses, or exceed a limit. Delete the
controls beside a defect (unless a renderer uses that working form as its workaround),
duplicates, and checks nothing depends on; record what the code relied on silently as new
rules; make limits bound rules `engine/limits.py` reads. OrientDB: 168 unlinked checks, 115
deleted, 43 new rules, 4 limit rules.

**6b. Retire defects by a guards-off campaign** (`e1748a4`). Run `--no-known-bugs` on
mousedeer and read the pair-level statistics the campaign records:
`known_bugs_run` / `known_bugs_exposed` (pairs one defect alone marks, and how many of them
failed), `failing_pairs_mixed`, `failing_pairs_unmarked`, and the per-pair log
`<tag>-failing-pairs.jsonl`. A defect whose marked pairs never fail is a candidate; **then
run a targeted campaign** that turns on the axes the defect needs (OrientDB `p4t`:
`--narrow-type-probability 0.8 --composite-index-probability 0.6 --index-probability 0.7
--write-cases 16 ...`) before retiring it. Classify every unmarked failing pair: a timeout,
a defect that should not have been retired, or a new finding. A defect the design avoids
can't be measured this way; retire it by reasoning only with the user's agreement.
OrientDB retired O3, O4, O11, O28, O29, O30, O32, O33, O34, O42; kept O23, O41, O43
unmeasured; restored O12, O17 and O37 when campaigns refuted their retirement.

**6c.** Delete the probe statements and fixtures no check reads afterwards (`19ecc0e`).

## Step 7. Every data model's write is the engine's

No statement may be built from the Instance's evaluation of the filter (OrientDB's doc
data model had three such forms: `eids`, `deref`, `rewrite`). Find each data model's
engine-evaluated form, probing the candidates first (OrientDB `4c675e7`):

- doc edge-attribute write: one `UPDATE src SET e[k].f = ... WHERE e.size() > k AND
  p(e[k])` per list position (the filtered `e[p].f = ...` is a silent no-op), the number
  of positions **asked of the database** before the write (`SELECT max(e.size())`): the
  Instance can drift from what every data model holds;
- doc edge delete: the bracket form `e = e[NOT (p)]` only;
- doc node-delete cascade: delete, then `e = e[target.id IS NOT NULL]` (a link to a
  deleted record reads null);
- a definedness test on an element through its keys (`e[k].keys() CONTAINS 'f'`;
  `e[k].f IS DEFINED` holds for an absent field, an O8 face);
- untyped embedded numbers: arithmetic through `asLong()`.

Each relied-on behaviour becomes a rule with a probe check (OrientDB C339-C343).

## Step 8. Dead code

`vulture --min-confidence 60` over the provider, each candidate checked by grep across the
repo (tests and the other providers included: a name the CLI calls dynamically, an enum
member the mutation check uses as another answer, and re-exports are not dead).

## Verification after every step

```bash
pytest tests/ -q -n auto                       # offline
python3 -m ganda <engine> rules --write        # re-render RULES.md (line numbers move)
python3 -m ganda <engine> probe                # live; writes the baseline when every rule holds
python3 -m ganda <engine> rules --mutate       # 0 survivors
```

Then two campaigns on mousedeer (synced to their own directory, deleted after):

- guards on, the reference run: OrientDB `run --iterations 12 --seed 1000 --workers 2
  --date-map-probability 0.5 --list-index-probability 0.5 --inheritance-probability 0.5`;
  expect 0 setup asymmetries, 0 internal errors, 0 write-state diffs, and only the known
  findings;
- guards off: `run --iterations 24 --seed 2000 --workers 4 --no-known-bugs --write-cases
  16` with the type, index, collation, inheritance and churn axes on; classify every
  unmarked failing pair and every write-state diff (regenerate the write offline and ask
  `write_known` whether a guard names it).

## Lessons

- **A retirement needs a campaign that exercises the defect**, not one that happened not
  to: O12 and O17 showed only in the targeted run, O37 only with more writes per seed.
- **A shared spelling label is not the same question.** `rel:pull_up` keeps the source
  predicate on the class while `graph:pull_up` and `doc:pull_up` lift it over a
  projection; comparing them compared a transformed query with an untransformed one
  (campaign p3, seed 1008). Check that every data model renders a spelling as the same
  transformation.
- **The Instance drifts.** A write the engine applies differently from Ganda's model makes
  every data model disagree with the Instance alike (invisible to the oracle, by design).
  Nothing a data model sends may depend on it (step 7); it may only steer generation.
- **Validity is not equivalence.** Removing the equivalence conditions exposed parameters
  no engine takes (an infinite pivot, an integer past the long range); keep those checks.
- **Replays must draw as the campaign drew.** A write-state bundle now records
  `keep_known_bugs` for a guards-off campaign; before, its repro drew other writes.
- **Probe every form before relying on it**, and record what you rely on as a rule with a
  check: the probe found the filtered list-element no-op, the past-the-end count and the
  element `IS DEFINED` face before (or right after) a campaign did.

## Open for every provider

- **Timeouts count as findings** at the default scale (OrientDB's recurring relational
  3-hop findings): treat a timeout as no answer at every scale, as the large scale does.
- **Index-path findings.** Most guarded defects differ because one data model has an index
  and the other not; classify findings by whether they survive identical physical design.
- **Framing.** On OrientDB and ArangoDB, doc and rel are encodings in one query language
  over one planner; HANA's separate engines carry the multi-model claim more directly.
- The core still calls the per-case material `rewrites` (`build_rewrites`,
  `rewrite_params`) and keeps `predicates.reverse` for HANA and ArangoDB; rename and remove
  them once both are ported.

## Per engine

**ArangoDB.** Done 2026-10-25 (the `arangodb-port` branch merged first): steps 4-8 as
above, plus the IR definitions dropped from the catalog, the readers of a branch-only
answer turned into `assumes` of the exact answer (the mutation check otherwise lets
another answer that takes the same branch survive), and the core mutation check finding
a provider through the registry. The first campaign found A43 (a sparse index serving a
correlated strict bound) and a renderer bug (`UNION()` of one class). Step 6b's evidence:
A40 0 of 236 marked pairs failing (a retirement candidate: its shape withholds every
data model alike); A1, A2, A41 are avoided by design.

**HANA.** Catalog exists (stage 1a), stages 2b-2d pending. Step 4a is its largest change
(the hint profiles and the rel-against-rel forms). It has a native relational model, four
data models (doc, rel, graph, rdf) and the provider options `compare_despite_errors` and
`partial_traversals` the pairing must keep working with. Its `oracle/rewrites.py`
goes in step 5 (`HanaSemantics.arith_exact` delegates to it until then). Live
only from hana-fuzzer; the offline runner (`tests.hana.run_offline`) before every live
check.
