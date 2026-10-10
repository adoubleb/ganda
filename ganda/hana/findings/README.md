# SAP HANA findings — for independent verification

Everything the Ganda SAP HANA fuzzer turned up during the live bring-up against
**HANA Cloud `4.00.000.00.1788859455`** (`SYS.M_DATABASE.VERSION`), 2026-10-09, sorted into
three buckets so you can confirm each judgement yourself:

- **A — actual HANA bug** (candidate engine defect): HANA does something wrong or crashes;
  behaviour is not what SAP's docs or the SQL/SPARQL standards say. Six of these (H1-H6),
  each with a self-contained repro script.
- **B — HANA quirk** (documented or by-design behaviour, or an undocumented limitation that
  refuses cleanly): HANA is within its rights; the provider adapts. Each B entry carries a
  verdict, checked 2026-10-10 against SAP's QRC 3/2026 docs and re-probed live.
- **C — Ganda (provider) bug**: our renderer emitted something wrong; HANA was right to reject
  it. No engine involvement. Listed for completeness; the fix is in the provider.

Every repro here needs only `hdbcli` and the `HANA_*` environment (`set -a; source ../../../.env;
set +a` from this folder, or export `HANA_HOST/PORT/USER/PASSWORD` and, through the tunnel,
`HANA_SNI_HOSTNAME`). Run each with `python3 -I <script>`. The `.md` next to each repro states
expected vs actual and cites the SAP doc / standard where relevant.

## How to run them all

```bash
cd ganda/hana/findings
set -a; source ../../../.env; set +a      # or export HANA_* yourself
for f in A*/repro.py B*/repro.py; do echo "### $f"; python3 -I "$f"; echo; done
```

---

## A — actual HANA bugs (candidate defects)

| id | one-line | repro | verdict to confirm |
|----|----------|-------|--------------------|
| **A1 / H1** | SPARQL serialises an `xsd:double` to ~6 significant figures, though it stores the value exactly (`STR(?d)` returns all 17 digits). | `A1-sparql-double-precision/` | Is losing precision in the result serialiser a defect? (The value is intact in storage — compare the `?d` term vs `STR(?d)`.) |
| **A2 / H2** | The Document Store CRASHES (`[3] fatal error: Execution flow must not reach here`) on a correlated scalar subquery whose inner query reads a collection through `UNNEST`. | `A2-docstore-crash-correlated-unnest/` | An internal assertion (code 3), not a clean "feature not supported" (code 7). The LEFT JOIN rewrite answers. Is the crash acceptable? |
| **A3 / H3** | SPARQL `SUM` / `AVG` over a group holding an unbound value answer UNBOUND once the predicate IRI has held a second datatype anywhere in the store (another class, another graph, a graph since dropped); with one datatype they skip it. `MAX` / `COUNT` skip it either way. | `A3-sparql-sum-unbound-mixed-datatypes/` | Arguable under SPARQL 1.1 whether SUM skips or propagates an unbound value; depending on data the query never reads (or already deleted) is not. |
| **A4 / H4** | SQL over Document Store collections: a `SUM` over a join that matches NO row fails with `[314] numeric overflow`, adding up values of links the join excludes; column tables (or `SUM(TO_DECIMAL(...))`) answer NULL. Fires only when the link view reads the link whole. | `A4-docstore-sum-overflow-excluded-rows/` | A spurious error for a query whose answer is NULL, depending on which columns a view reads. |
| **A5 / H5** | SQL over a Document Store field: `f BETWEEN ? AND ?` types its PARAMETERS BIGINT and fails on a string (`[-10427]`), while `f >= ? AND f <= ?` (what BETWEEN means) answers; the field holds strings only. Was B9 ("does not minimise": it does, with bound parameters). | `A5-docstore-between-types-parameters-bigint/` | One form answers and its definition fails, on the same field and parameters; independent of the data. |
| **A6 / H6** | SPARQL refuses a legal FILTER or BIND over a variable no pattern binds (`[129] FILTER(s) contains unbound variables`) -- even `FILTER(!BOUND(?x))`, true for every solution per SPARQL 1.1; `COALESCE(?x, 1)` is accepted. Was B10. | `A6-sparql-filter-unbound-variable-refused/` | W3C SPARQL 1.1 §17, §18.2.1 allow it; SAP does not document the restriction. A conformance defect (a legal query refused), not a wrong answer. |

## B — HANA quirks (by-design / documented; the provider adapts)

| id | behaviour | repro | verdict (2026-10-10) |
|----|-----------|-------|----------------------|
| **B1** | SQL `MEDIAN` answers in the argument's type: over integers the mean of the two middle values ROUNDED (`{1,10}` → 6; the first write-up's "upper middle" was wrong). | `B1-median-no-interpolation/` | **Documented** (the MEDIAN page's own example rounds 2.5 to 3). |
| **B2** | SPARQL `MEDIAN` needs a `GROUP BY` ("Unsupported functionality ... without GROUP BY"); the sibling extension aggregates VAR and MODE do not. | `B2-sparql-median-needs-groupby/` | **Undocumented limitation**, inconsistent with VAR / MODE and SPARQL's implicit group: a doc / feature gap, not a wrong answer. |
| **B3** | The Document Store refuses a BOOLEAN constant (`CASE … THEN TRUE`) in a query that reads an NVARCHAR collection ("please use TO_JSON_BOOLEAN()"); the cast must live one CTE removed. | `B3-boolean-constant-in-collection-query/` | **Documented** (TO_JSON_BOOLEAN). |
| **B4** | A collection filter's `LIKE` takes the documented `CAST(? AS NVARCHAR)`, but refuses `CAST(? AS NVARCHAR(5000))` ("must be a constant string literal ..."); the first write-up tested only the length form. | `B4-like-rhs-constant/` | **Not a bug**; the length form's refusal is undocumented. |
| **B5** | A doc `UPDATE` combines `SET ..., UNSET ...` with a comma (the documented example; without it, a syntax error); a `SET` takes values, not `$."f" + c`. | `B5-doc-update-set-unset-and-arith/` | **Not a bug** (the docs' grammar line omits the comma: a doc slip). |
| **B6** | The Document Store refuses an `UNNEST`-based view nested inside a subquery (`IN (…)`; an EXISTS over a double-UNNEST view; an EXISTS under OR / NOT or next to UNNEST link views): "unsupported form of subquery with UNNEST". | `B6-unnest-view-in-subquery/` | **Undocumented limitation**, refused cleanly (code 7): a doc / feature gap. |
| **B7** | A `GROUP BY` inside a subquery over a collection is refused: "GROUP BY clause after GROUP BY clause on collection tables". | `B7-groupby-subquery-over-collection/` | **Undocumented limitation**, refused cleanly (code 7). |
| **B8** | `SPARQL_TABLE` types a column after its predicate's datatype history across the whole store: one datatype → typed; once the predicate has held a second one (another graph, even dropped) → NVARCHAR (`'7'`, `'1'`). The first write-up blamed OPTIONAL. | `B8-sparql-table-nullable-numeric-nvarchar/` | **Documented core** (several datatypes → NVARCHAR); its store-wide scope and `'1'` / `'0'` booleans are not. Same history as A3 / H3. |
| **B12** | The collection query's `MAX` / `MIN` refuse a string field (code 129, "max supports only numeric values"), even over `TO_NVARCHAR`; SQL over the views answers. | (one statement: `SELECT MAX($."f") FROM c` over string values) | **Documented, implicitly**: aggregates over collection fields take the numeric casts (TO_BIGINT / TO_DOUBLE / TO_DECIMAL). |
| **B11** | `START` is a reserved word, so it cannot be a bare column alias (syntax error 257). | `B11-start-reserved-word/` | **Documented** (reserved-words list). |

## C — Ganda (provider) bugs (no engine involvement)

These were our renderer's mistakes; HANA was right. No repro script — the fix is in the named
file. Listed so you can see what was NOT a HANA issue.

| id | what the provider did wrong | fixed in |
|----|------------------------------|----------|
| **C1** | `ganda hana probe <id>` filtered out all facts: the CLI didn't pass the fact filter, so `probe.main` saw `['hana','probe']`. | `ganda/cli.py`, `ganda/*/provider.py`, `ganda/core/provider.py` |
| **C2** | Several probe facts tested SQL forms the provider never emits (combined SET/UNSET, a single-scope CASE, field-referencing arithmetic, bare-quoted paths) — probe bugs, not engine facts. | `ganda/hana/scripts/probe.py` |
| **C3** | Case-query value projections through `SPARQL_TABLE`/`rdf:table` lacked decode-suffix aliases, so ints came back as strings (B8 is the engine side; this is the provider not adapting). | `ganda/hana/concrete_syntax/select.py`, `traversal.py` |
| **C4** | The state check re-read an rdf double from `STR(?o)` only when the text "looked float"; `STR` prints `%.17g`, so 2^53 came back `9007199254740992` and the rounded term (`9.0072e+15`) was kept. Every seed holding 2^53 in a float field failed its state check and ran no case (campaign-20261009-095838: 14 of 16 seeds). | `ganda/hana/connection.py` (`_exact`) |
| **C5** | C3 was incomplete: booleans (`"0"` / `"1"`), group keys and values, method answers and int `max` / `min` / `sum` reached `rdf:table_fn` without a decode suffix; all 10 mismatches of campaign-20261009-095838. The rule behind it (B8, corrected): SPARQL_TABLE types a column after the datatypes its predicate holds across the graph. | `concrete_syntax/select.py`, `traversal.py` (`agg_proj`, `_rdf_proj`) |
| **C6** | The rdf sum guard `IF(COUNT(?w) > 0, SUM(?w), ?ganda_unknown)` names a bare variable in a grouped SELECT, which SPARQL 1.1 forbids (`[129] Non GROUP BY variable in SELECT`): every rdf variant of a traversal or grouped select over `sum` failed. Now `... , MAX(?w))`, unbound over no value. | `concrete_syntax/traversal.py` (`guarded`), `select.py` |
| **C7** | The doc write's read-back `WHERE $."id" IN (?, ?)` left its markers uncast ("please specify the types for parameter"). | `concrete_syntax/write.py`, `connection.py` (`run_statements`) |
| **C8** | The rdf write read-backs (`returning before / after`) projected a double term, rounded by H1 (pi as `3.14159`). Found offline once the stand-in rounded doubles as HANA does. | `concrete_syntax/write.py` (`_rdf`) |
| **C9** | The H2 and B6 guards were incomplete: a CONTAINSALL over the doc views is a correlated scalar subquery over UNNEST (H2, a crash in selects and traversals alike), and an EXISTS over a node's element view is refused next to the UNNEST edge views of a traversal (B6). The views now ask CONTAINSALL as one EXISTS per element, and a doc traversal withholds a LIST membership. | `concrete_syntax/predicate.py`, `document.py`, `traversal.py` |
| **C10** | The doc views asked a LIST membership under OR / NOT (an EXISTS over an element view there): the Document Store refuses it (B6), 14 error asymmetries in campaign-20261009-165805. The views now ask a membership only as a positive conjunct. | `concrete_syntax/document.py` (`views_ask`), `select.py`, `write.py` |

---

## Notes on method

- A finding is an **engine-bug candidate (A)** only when HANA departs from its own docs or the
  standard (a crash, a wrong value, an internal error for a legal query). When HANA merely has a
  stricter grammar or a documented behaviour, it is a **quirk (B)** and the provider adapts.
- The B/A boundary was checked 2026-10-10 against SAP's docs (QRC 3/2026) and W3C SPARQL 1.1, and
  re-probed live: B9 and B10 moved to A (A5, A6); B1, B4, B5 and B8 kept B with corrected claims.
- Code 129 ("transaction rolled back by an internal error") is how the triple store reports EVERY
  refusal (a syntax error too), and how the Document Store reports a string MAX: it marks no
  internal failure there (`connection.REJECTION_129`).
- The campaign's own bug bundles (`bugs/`) are the raw evidence (every variant's text, binds,
  rows, mismatches); this folder distils them into runnable, minimal repros.
- `TRANSCRIPT.txt` is a captured run of every repro here. `campaign-round4-clean-stats.json` is the
  campaign that, after all the provider workarounds landed, ran **21 cases, 0 findings, 0 crashes,
  0 internal errors** on seed 1 — i.e. the A/B behaviours are all either worked around or filed,
  and nothing spurious remains.
