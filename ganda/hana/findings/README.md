# SAP HANA findings — for independent verification

Everything the Ganda SAP HANA fuzzer turned up during the live bring-up against
**HANA Cloud `4.00.000.00.1788859455`** (`SYS.M_DATABASE.VERSION`), 2026-10-09, sorted into
three buckets so you can confirm each judgement yourself:

- **A — actual HANA bug** (candidate engine defect): HANA does something wrong or crashes;
  behaviour is not what SAP's docs or the SQL/SPARQL standards say. Two of these (H1, H2),
  each with a self-contained repro script.
- **B — HANA quirk** (documented or by-design behaviour, just not what a naive port assumed):
  HANA is within its rights; the provider adapts. Verify by reading the repro and deciding
  whether the behaviour is acceptable/expected.
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
for f in A-*/repro.py B-*/repro.py; do echo "### $f"; python3 -I "$f"; echo; done
```

---

## A — actual HANA bugs (candidate defects)

| id | one-line | repro | verdict to confirm |
|----|----------|-------|--------------------|
| **A1 / H1** | SPARQL serialises an `xsd:double` to ~6 significant figures, though it stores the value exactly (`STR(?d)` returns all 17 digits). | `A1-sparql-double-precision/` | Is losing precision in the result serialiser a defect? (The value is intact in storage — compare the `?d` term vs `STR(?d)`.) |
| **A2 / H2** | The Document Store CRASHES (`[3] fatal error: Execution flow must not reach here`) on a correlated scalar subquery whose inner query reads a collection through `UNNEST`. | `A2-docstore-crash-correlated-unnest/` | An internal assertion (code 3), not a clean "feature not supported" (code 7). The LEFT JOIN rewrite answers. Is the crash acceptable? |

## B — HANA quirks (by-design / documented; the provider adapts)

| id | behaviour | repro | what to confirm |
|----|-----------|-------|-----------------|
| **B1** | `MEDIAN` over integers does NOT interpolate an even count (returns the upper-middle value); over doubles it does. The provider uses `PERCENTILE_CONT(0.5)` for a true statistical median. | `B1-median-no-interpolation/` | Is this HANA's documented MEDIAN semantics (type-dependent)? Decide if it's a quirk or a doc gap. |
| **B2** | SPARQL `MEDIAN` requires a `GROUP BY` — an internal error (code 129) without one, even though it interpolates fine with one. | `B2-sparql-median-needs-groupby/` | Is "MEDIAN needs GROUP BY" documented? Borderline between quirk and bug (an internal error for a legal-looking query). |
| **B3** | The Document Store rejects a BOOLEAN constant (`CASE … THEN TRUE`) in any query that reads a collection ("invalid constant, please use TO_JSON_BOOLEAN()"); the cast must live one CTE removed from the collection. | `B3-boolean-constant-in-collection-query/` | Documented restriction on the Document Store SQL dialect? |
| **B4** | A collection-filter `LIKE` right-hand side must be a constant string literal (not a parameter): "Right-hand side of LIKE must be a constant string literal or TO_NVARCHAR, TO_CHAR, UPPER and LOWER functions". | `B4-like-rhs-constant/` | Documented grammar restriction? |
| **B5** | The Document Store refuses a combined `UPDATE … SET … UNSET …` (syntax error 257), though each alone works; and a doc UPDATE `SET` takes only constant expressions (no `$."f" = $."f" + c`). | `B5-doc-update-set-unset-and-arith/` | Documented UPDATE grammar? |
| **B6** | The Document Store refuses an `UNNEST`-based view nested inside a subquery (`IN (…)`, a correlated EXISTS over a double-UNNEST edge view): "unsupported form of subquery with UNNEST". A single-UNNEST node view inside EXISTS is fine. | `B6-unnest-view-in-subquery/` | Documented limitation of UNNEST in subqueries? |
| **B7** | A `GROUP BY` inside a subquery over a collection is refused: "GROUP BY clause after GROUP BY clause on collection tables (perhaps due to a subquery)". | `B7-groupby-subquery-over-collection/` | Documented? |
| **B8** | `SPARQL_TABLE` drops the datatype of a maybe-unbound numeric variable (NVARCHAR column, `"7"`), while the same query run directly through `SPARQL_EXECUTE` types it `xsd:long`. | `B8-sparql-table-nullable-numeric-nvarchar/` | Documented SPARQL_TABLE typing rule, or a quirk? |
| **B9** | In a multi-view doc join, a STRING field with a numeric-looking value (`"0"`) was inferred numeric, so a string `BETWEEN 'A' AND 'i'` failed converting to BIGINT. **Context-dependent — does not minimise** (the isolated repro succeeds); the fix made the campaign run 0 findings. | `B9-string-field-numeric-inference/` | Weakest finding; evidence is the campaign bundle, not a one-liner. Likely a Document Store type-inference quirk; you decide. |
| **B10** | HANA rejects a SPARQL FILTER that names a variable bound nowhere in the pattern (`[129]` internal error), rather than treating it as a per-solution evaluation error. The provider declares its never-bound sentinel with a never-matching OPTIONAL. | `B10-filter-unbound-variable/` | SPARQL 1.1 says an unbound variable in FILTER is a per-solution type error; a whole-statement internal error is arguably non-conforming. Quirk or bug? |
| **B12** | The collection query's `MAX` / `MIN` refuse a string field with code 129 ("max supports only numeric values"); SQL over the views and the collection's `ORDER BY ... LIMIT 1` answer. The provider asks a string extremum through the views. | (probe in the session notes; one statement: `SELECT MAX($."f") FROM c` over string values) | Documented restriction? An internal-error code for a type refusal is odd, but the message is clear. |
| **B11** | `START` is a reserved word, so it cannot be a column alias (syntax error 257). | `B11-start-reserved-word/` | Documented reserved word? (Trivial quirk; listed for completeness.) |

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

---

## Notes on method

- A finding is an **engine-bug candidate (A)** only when HANA departs from its own docs or the
  standard (a crash, a wrong value, an internal error for a legal query). When HANA merely has a
  stricter grammar or a documented behaviour, it is a **quirk (B)** and the provider adapts.
- The B/A boundary is a judgement call for B2, B9, B10 — flagged above. That is exactly what you
  asked to verify personally.
- The campaign's own bug bundles (`bugs/`) are the raw evidence (every variant's text, binds,
  rows, mismatches); this folder distils them into runnable, minimal repros.
- `TRANSCRIPT.txt` is a captured run of every repro here. `campaign-round4-clean-stats.json` is the
  campaign that, after all the provider workarounds landed, ran **21 cases, 0 findings, 0 crashes,
  0 internal errors** on seed 1 — i.e. the A/B behaviours are all either worked around or filed,
  and nothing spurious remains.
