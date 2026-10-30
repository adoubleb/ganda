"""ArangoDB's engine rules (`ganda.core.rules`): what the fuzzer acts on so that it asks
and compares only what the engine's execution paths answer alike. Its three data models
speak one language, AQL, so a subject is an execution path: a collection scan (a FOR
loop, its FILTER, COLLECT and the functions), the same statement served by a persistent
index, the graph traversal operator, the path functions, a stream transaction and the
REST document API. The probe facts that check the rules and reproduce the defects are
in `checks.py` (`probe.py` runs them).

The defects -- those that can make two data models differ: **A25** (the legacy port's,
found again by this port's first campaign with the catalog: an OR of two whole-array
equalities on a LIST attribute -- `replace-or-with-in` -- is served from the expanded array
index's ELEMENT keys and answers nothing; withheld by `known_bugs/predicate.py` on the data
models holding the index); **A42** (found by the large-scale
campaign of 2026-10-24, seed 6: `LIKE` with a wildcard over a stored string of 125 000
code points or more fails the query -- errorNum 1543, "invalid regex value": the `%` is
spelled `(.|[\\r\\n])*`, which backtracks one frame per character into ICU's stack
limit -- where `==`, `CONTAINS`, `STARTS_WITH` and `LENGTH` over the same value answer;
withheld by `known_bugs/predicate.py` where a column the LIKE reads holds such a value, the
large scale's grown strings); **A43** (a correlated strict bound served by a sparse
persistent index loses the null and absent records; withheld there). Retired
2026-10-25, as unable to make two data models differ (each answers alike on every one):
A1 and A2 (a stored `-0.0`: the pools still hold none), A40 (`NOT (x IN n.list)` over a
non-array; a guards-off campaign marked 236 pairs and none failed) and A41 (a vertex
filter moved into a traversal evaluated on the start vertex: the `IS_OBJECT` ternary
stays). The legacy
port's other findings (`../arangoDB/issue-drafts/`, A3..A39) are outside this surface
(ArangoSearch views, MDI and geo indexes, the JavaScript door) or unreachable by
construction: no ordered comparison on a string column (A9, A14, A34: `order.string` is
collation), no `JoinNode` over a top-level two-loop (A20); A24's pair (an expanded index
with `storedValues`) is never built.
"""
from __future__ import annotations

import enum

from ganda.core.rules.model import Defect, ErrorClass, ErrorKind, Kind, Question, Rule, Verdict
from ganda.core.rules.vocabulary import (
    MAX_EMPTY,
    OWN_UNCOMMITTED_VISIBLE,
    PARALLEL_EDGE_ROWS,
    STRING_ORDER,
    SUM_EXACT_BELOW,
    UNDIRECTED_SELF_LOOP_ROWS,
    VARIABLE_LENGTH_PATH_MODE,
    EmptyAggregate,
    PathMode,
    StringOrder,
)


class Subject(enum.StrEnum):
    AQL = "aql"  # a collection scan: FOR, FILTER, COLLECT, the functions, a data-modification statement
    AQL_INDEX = "aql@index"  # the same statement served by a persistent index (the `id` index, a hint, the array index)
    TRAVERSAL = "traversal"  # the graph traversal operator, `FOR v, e, p IN min..max OUTBOUND start edges`
    PATH = "path"  # SHORTEST_PATH, K_SHORTEST_PATHS, ALL_SHORTEST_PATHS
    TRANSACTION = "stream-transaction"  # /_api/transaction: the transaction door, a committed setup
    DOCUMENT_API = "document-api"  # /_api/document: the bulk insert, PATCH, PUT, DELETE


AQL, AQL_INDEX, TRAVERSAL, PATH, TRANSACTION, DOCUMENT_API = Subject
COMPARE, ASK, EQUIVALENT, EXACT = Kind.COMPARE, Kind.ASK, Kind.EQUIVALENT, Kind.EXACT
REFUSED, ACCEPTED = Verdict.REFUSED, Verdict.ACCEPTED


def accepts(construct: str) -> Question:
    return Question(f"accepts.{construct}", Verdict, ASK)


def equivalent(form: str) -> Question:
    return Question(f"equivalent.{form}", bool, EQUIVALENT)


def exact(value: str, domain=bool) -> Question:
    return Question(f"exact.{value}", domain, EXACT)


# --- the engine's own questions ------------------------------------------------------------------
STRING_EQUALITY = Question("equality.string", StringOrder, COMPARE)
MAX_MIN_SKIP_NULL = Question("aggregate.max_min.skips_null", bool, COMPARE)
SHORTEST_PATH_UNREACHABLE = Question("shortest_path.unreachable.result", int, COMPARE)
SHORTEST_PATH_SAME_RECORD = Question("shortest_path.same_record.result", int, COMPARE)
UPDATE_MERGES_MAP_VALUE = Question("update.map_value.merged_by_default", bool, COMPARE)
UPDATE_KEEPS_NULL = Question("update.null.kept_by_default", bool, COMPARE)
DELETE_VERTEX_CASCADES = Question("delete_vertex.cascades_edges", bool, COMPARE)
INLINE_ARRAY_REBUILDS = Question("update.inline_array.rebuilds_elements", bool, COMPARE)
ARITHMETIC_EXACT_BELOW = exact("arithmetic.integer.below", int)

# --- compare: paths answering differently are not compared where the question is exercised ----
COMPARE_RULES = (
    Rule(STRING_ORDER, AQL, StringOrder.COLLATION),
    Rule(STRING_EQUALITY, AQL, StringOrder.CODE_POINT),
    Rule(MAX_EMPTY, AQL, EmptyAggregate.NULL),
    Rule(MAX_MIN_SKIP_NULL, AQL, True),
    Rule(PARALLEL_EDGE_ROWS, AQL, 2),
    Rule(PARALLEL_EDGE_ROWS, TRAVERSAL, 2),
    Rule(UNDIRECTED_SELF_LOOP_ROWS, AQL, 2),
    Rule(UNDIRECTED_SELF_LOOP_ROWS, TRAVERSAL, 2),
    Rule(VARIABLE_LENGTH_PATH_MODE, TRAVERSAL, PathMode.TRAIL),
    Rule(SHORTEST_PATH_UNREACHABLE, PATH, 0),
    Rule(SHORTEST_PATH_SAME_RECORD, PATH, 1),
    Rule(UPDATE_MERGES_MAP_VALUE, AQL, True),
    Rule(UPDATE_MERGES_MAP_VALUE, DOCUMENT_API, True),
    Rule(UPDATE_KEEPS_NULL, AQL, True),
    Rule(UPDATE_KEEPS_NULL, DOCUMENT_API, True),
    Rule(DELETE_VERTEX_CASCADES, AQL, False),
    Rule(INLINE_ARRAY_REBUILDS, AQL, True),
    Rule(OWN_UNCOMMITTED_VISIBLE, TRANSACTION, True),
)

# --- ask: a construct a path refuses is not rendered --------------------------------------------
ASK_RULES = (
    Rule(accepts("has.non_object_argument"), AQL, ACCEPTED),
    Rule(accepts("values.non_object_argument"), AQL, REFUSED),
    Rule(accepts("variable.redeclared"), AQL, REFUSED),
    Rule(accepts("bind_variable.undeclared"), AQL, REFUSED),
    Rule(accepts("transaction.collection_absent_from_database"), TRANSACTION, REFUSED),
)

# --- equivalent: twin forms answer alike, so a mismatch between them is a finding ----------------
EQUIVALENT_RULES = (
    Rule(equivalent("link_array_forms"), AQL, True),
    Rule(equivalent("junction_forms"), AQL, True),
    Rule(equivalent("flat_collect"), AQL, True),
    Rule(equivalent("edge_scan"), TRAVERSAL, True),
    Rule(equivalent("both_as_union"), TRAVERSAL, True),
    Rule(equivalent("deep_traversal"), TRAVERSAL, True),
    Rule(equivalent("reach_bfs_global"), TRAVERSAL, True),
    Rule(equivalent("path_forms"), PATH, True),
    Rule(equivalent("write_doors"), AQL, True),
    Rule(equivalent("update_replace"), AQL, True),
    Rule(equivalent("write_doors"), DOCUMENT_API, True),
)

# --- exact: where a value compares exactly ------------------------------------------------------
EXACT_RULES = (
    Rule(SUM_EXACT_BELOW, AQL, 2**53),
    Rule(ARITHMETIC_EXACT_BELOW, AQL, 2**53),
)

# the code compares no engine path with the IR: the catalog holds no IR definition
RULES = COMPARE_RULES + ASK_RULES + EQUIVALENT_RULES + EXACT_RULES

DEFECTS = (
    Defect("A25", AQL_INDEX),
    Defect("A42", AQL),
    Defect("A43", AQL_INDEX),
)

# The classification of an errorNum and its message; the HTTP status decides what the
# catalog does not: a 5xx the server ANSWERED is an error from inside the engine (a
# finding on its own) unless the manual documents it as a rejection.
ERRORS = (
    # the query killed at `maxRuntime`; a cursor gone before its next batch was fetched
    # (the query killed meanwhile, or its time to live over)
    ErrorClass(ErrorKind.TIMEOUT, frozenset({1500, 1600})),
    ErrorClass(ErrorKind.TIMEOUT, marks=("query killed", "timed out", "time out", "timeout"), ignore_case=True),
)
