"""ArangoDB's engine rules and the defects (A25, A39, A40, A42, A43, A44) that can make
two data models differ; `checks.py` maps the probe facts to them."""
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
    Defect("A39", AQL),
    Defect("A40", AQL),
    Defect("A42", AQL),
    Defect("A43", AQL_INDEX),
    Defect("A44", AQL),
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
