"""Plan steering: the same SQL text asked of another of SAP HANA's execution engines or
optimizer modes through a `WITH HINT (...)` clause -- a variant of its own on every data
model whose text is SQL (rel; the doc views; the graph wrappers around OPENCYPHER_TABLE;
rdf's SPARQL_TABLE). A hint never changes what a statement means (SQL Reference, "HINT
Details", QRC 3/2026): a hinted variant answering differently from its plain twin is an
engine defect, with no cross-model confound.

Each PROFILE is one coherent steering, the hint names SAP documents:

    nohex             NO_USE_HEX_PLAN: the classic engines instead of HEX (HANA Cloud's
                      default engine)
    hex               USE_HEX_PLAN: HEX preferred where the optimizer would leave it
    esx               USE_ESX_PLAN: the ESX engine
    olap              the OLAP (column search) engine, HEX avoided
    join_engine       neither HEX nor OLAP: the join engine
    row_ops           the classic plan with the ROW engine's sort, limit, filter, distinct,
                      aggregation, join and union operators (NO_CS_*)
    cs_ops            the classic plan with the COLUMN engine's operators (CS_*)
    rule_based        OPTIMIZATION_LEVEL(RULE_BASED): the rewriting rules, no cost-based
                      enumeration
    minimal           OPTIMIZATION_LEVEL(MINIMAL): the query as written, mandatory rules only
    no_rewrite        the logical transformations off (predicate simplification, constant
                      pre-evaluation, join removal, grouping and aggregation simplification;
                      PRESERVE_GROUPBY)
    disjunction_split DISJ_FILTER_TRANSFORMATION: a disjunctive filter split into a UNION ALL
    hex_disk          HEX_DISK: HEX's spill-to-disk operators
    nested_loop       HEX_NESTED_LOOP_JOIN
    hash_join         HEX_HASH_JOIN
    no_index          NO_INDEX_SEARCH, NO_INDEX_JOIN: scans and joins without the indexes
    serial            MAX_CONCURRENCY(1): single-threaded execution
    plan_cache        IGNORE_PLAN_CACHE: the statement compiled afresh, the cached plan
                      ignored (beside it, the `again` position runs the plain text a
                      second time, through the cached plan)

The probe (`ganda hana probe P`, run 2026-10-12 against HANA Cloud 4.00.000.00) runs
every profile over the statement shapes the renderers emit -- a CTE query, a derived
table with a LEFT JOIN and a correlated scalar subquery, LIMIT / OFFSET, a view over a
collection, OPENCYPHER_TABLE and SPARQL_TABLE -- and compares the rows with the plain
statement's; an unknown hint name is an error (`[468] hint error: invalid hint`), never
ignored, and `THROW_ERROR` blocks a statement (`[256]`), so the clause is always parsed.

One profile per question: the select catalog draws it with the other rewrites
(`select.Rewrites.hint`), a traversal or a path picks it from a hash of the question
(`choose`), so a case carries one hinted twin per data model, named by the profile
(`rel:olap`, `doc:nest+nohex`, `rel:hint_minimal`). `hinted` appends the clause to a
statement (after any LIMIT / OFFSET: the hint clause closes a SELECT); `strip` takes it
off again (the offline stand-in has no hints).
"""
from __future__ import annotations

import re
import zlib

from ganda.hana.rules import assumes
from ganda.hana.rules.catalog import COLLECTION, SQL

# a hinted twin answers as its plain statement does: a mismatch between them is a finding
assumes("equivalent.hint_profile", SQL, True)
assumes("equivalent.hint_profile", COLLECTION, True)

PROFILES = {
    "nohex": ("NO_USE_HEX_PLAN",),
    "hex": ("USE_HEX_PLAN",),
    "esx": ("USE_ESX_PLAN",),
    "olap": ("NO_USE_HEX_PLAN", "USE_OLAP_PLAN"),
    "join_engine": ("NO_USE_HEX_PLAN", "NO_USE_OLAP_PLAN"),
    "row_ops": ("NO_USE_HEX_PLAN", "NO_CS_SORT", "NO_CS_LIMIT", "NO_CS_FILTER", "NO_CS_DISTINCT", "NO_CS_AGGR", "NO_CS_JOIN", "NO_CS_UNION_ALL"),
    "cs_ops": ("NO_USE_HEX_PLAN", "CS_SORT", "CS_LIMIT", "CS_FILTER", "CS_DISTINCT", "CS_AGGR", "CS_JOIN", "CS_UNION_ALL"),
    "rule_based": ("OPTIMIZATION_LEVEL(RULE_BASED)",),
    "minimal": ("OPTIMIZATION_LEVEL(MINIMAL)",),
    "no_rewrite": ("NO_PREDICATE_SIMPLIFICATION", "NO_CONST_PREEVALUATION", "NO_JOIN_REMOVAL", "NO_GROUPING_SIMPLIFICATION", "NO_AGGR_SIMPLIFICATION", "PRESERVE_GROUPBY"),
    "disjunction_split": ("DISJ_FILTER_TRANSFORMATION",),
    "hex_disk": ("HEX_DISK",),
    "nested_loop": ("HEX_NESTED_LOOP_JOIN",),
    "hash_join": ("HEX_HASH_JOIN",),
    "no_index": ("NO_INDEX_SEARCH", "NO_INDEX_JOIN"),
    "serial": ("MAX_CONCURRENCY(1)",),
    "plan_cache": ("IGNORE_PLAN_CACHE",),
}
NAMES = tuple(PROFILES)
# the data models whose texts a hint clause closes (rdf: its `table_fn` position only;
# doc: the views, and the collection query itself where the store asks the question --
# the Document Store takes the clause, probe D27)
DATA_MODELS = ("rel", "doc", "graph", "rdf")

_CLAUSE = re.compile(r"\s+WITH HINT \((?:[^()]|\([^()]*\))*\)$")


def clause(profile: str) -> str:
    return f" WITH HINT ({', '.join(PROFILES[profile])})"


def hinted(text: str, profile: str) -> str:
    """A statement with the profile's hint clause appended."""
    return text + clause(profile)


def strip(text: str) -> str:
    """A statement without its hint clause (none: unchanged)."""
    return _CLAUSE.sub("", text)


def is_hinted(text: str) -> bool:
    return _CLAUSE.search(text) is not None


def choose(key: str) -> str:
    """The profile a question gets where no random stream is at hand: stable in the key."""
    return NAMES[zlib.crc32(key.encode("utf-8")) % len(NAMES)]
