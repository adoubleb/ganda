"""SAP HANA's engine rules (`ganda.core.rules`): what the fuzzer acts on so that it asks
and compares only what HANA's languages answer alike. The probe facts that check them
are in `checks.py`."""
from __future__ import annotations

import enum

from ganda.core.rules.model import Defect, ErrorClass, ErrorKind, Kind, Question, Rule, Verdict
from ganda.core.rules.vocabulary import (
    ABSENT_IS_NULL,
    BARE_PARAMETER_TYPED_BY,
    COUNT_EMPTY,
    DISCONNECTED_PATTERNS,
    DOUBLE_EXACT_ROUND_TRIP,
    EDGE_BINDING,
    EMPTY_STRING_IS_NULL,
    FILTER_LOGIC,
    GROUPED_EMPTY_ROWS,
    IR_RULES,
    JOINS_SQL_TRANSACTION,
    LIKE_CASE_SENSITIVE,
    MAX_EMPTY,
    MEMBERSHIP_ABSENT_LIST,
    NULL_KEY_GROUPS,
    NULLS_ASC,
    NULLS_DESC,
    OWN_UNCOMMITTED_VISIBLE,
    PARALLEL_EDGE_ROWS,
    PEER_READ_COMMITTED,
    SKIPS_NULL,
    STRING_ORDER,
    STRING_PADDING,
    SUM_ALL_NULL,
    SUM_EMPTY,
    SUM_EXACT_BELOW,
    UNDIRECTED_SELF_LOOP_ROWS,
    UNGROUPED_EMPTY_ROWS,
    VARIABLE_LENGTH_PATH_MODE,
    WRITE_ROW_COUNT,
    Combination,
    EdgeBinding,
    EmptyAggregate,
    EmptyInputRows,
    Logic,
    NullPlacement,
    Padding,
    PathMode,
    StringOrder,
    Truth,
    TypedBy,
)


class Subject(enum.StrEnum):
    SQL = "sql"
    SQL_DOC = "sql@doc"
    SQL_GRAPH = "sql@graph"
    COLLECTION = "collection"
    SPARQL = "sparql"
    OPENCYPHER = "opencypher"
    GRAPHSCRIPT = "graphscript"
    SQLSCRIPT = "sqlscript"


class AvgResult(enum.StrEnum):
    DECIMAL_25_6_TRUNCATED = "decimal(25,6)-truncated"
    DOUBLE = "double"


class MedianResult(enum.StrEnum):
    INTERPOLATED = "interpolated"
    ROUNDED_HALF_AWAY_FROM_ZERO = "rounded-half-away-from-zero"


SQL, SQL_DOC, SQL_GRAPH, COLLECTION, SPARQL, OPENCYPHER, GRAPHSCRIPT, SQLSCRIPT = Subject
COMPARE, ASK, EQUIVALENT, EXACT = Kind.COMPARE, Kind.ASK, Kind.EQUIVALENT, Kind.EXACT
REFUSED, ACCEPTED = Verdict.REFUSED, Verdict.ACCEPTED


def accepts(construct: str) -> Question:
    return Question(f"accepts.{construct}", Verdict, ASK)


def equivalent(form: str) -> Question:
    return Question(f"equivalent.{form}", bool, EQUIVALENT)


def exact(value: str, domain=bool) -> Question:
    return Question(f"exact.{value}", domain, EXACT)


AVG_INTEGER = Question("aggregate.avg.integer.result", AvgResult, COMPARE)
MEDIAN_FUNCTION_INTEGER = Question("aggregate.median_function.integer.result", MedianResult, COMPARE)
PERCENTILE_CONT_INTEGER = Question("aggregate.percentile_cont_half.integer.result", MedianResult, COMPARE)
UNLABELLED_VERTEX_SPANS = Question("match.unlabelled_vertex.spans_all_tables", bool, COMPARE)
NODE_WITH_TWO_TYPES_ROWS = Question("query.node_with_two_types.rows", int, COMPARE)
CROSS_TYPE_EXACT_BELOW = exact("cross_type_numeric_pivot.below", int)

# --- compare: subjects answering differently are not compared where the question is exercised ---
COMPARE_RULES = (
    Rule(NULLS_ASC, SQL, NullPlacement.FIRST),
    Rule(NULLS_ASC, COLLECTION, NullPlacement.FIRST),
    Rule(NULLS_ASC, SPARQL, NullPlacement.FIRST),
    Rule(NULLS_ASC, OPENCYPHER, NullPlacement.LAST),
    Rule(NULLS_DESC, SQL, NullPlacement.LAST),
    Rule(NULLS_DESC, COLLECTION, NullPlacement.LAST),
    Rule(NULLS_DESC, SPARQL, NullPlacement.LAST),
    Rule(NULLS_DESC, OPENCYPHER, NullPlacement.FIRST),
    Rule(STRING_ORDER, SQL, StringOrder.UTF16_CODE_UNIT),
    Rule(STRING_ORDER, COLLECTION, StringOrder.UTF16_CODE_UNIT),
    Rule(STRING_ORDER, SPARQL, StringOrder.CODE_POINT),
    Rule(STRING_ORDER, OPENCYPHER, StringOrder.UTF16_CODE_UNIT),
    Rule(ABSENT_IS_NULL, SQL, True),
    Rule(ABSENT_IS_NULL, COLLECTION, True),
    Rule(ABSENT_IS_NULL, SPARQL, True),
    Rule(ABSENT_IS_NULL, OPENCYPHER, True),
    Rule(EMPTY_STRING_IS_NULL, SQL, False),
    Rule(STRING_PADDING, SQL, Padding.NO_PAD),
    Rule(DOUBLE_EXACT_ROUND_TRIP, SQL, True),
    Rule(DOUBLE_EXACT_ROUND_TRIP, COLLECTION, True),
    Rule(DOUBLE_EXACT_ROUND_TRIP, SPARQL, False),
    Rule(DOUBLE_EXACT_ROUND_TRIP, OPENCYPHER, True),
    Rule(FILTER_LOGIC, SQL, Logic.KLEENE),
    Rule(FILTER_LOGIC, COLLECTION, Logic.KLEENE),
    Rule(FILTER_LOGIC, SPARQL, Logic.KLEENE),
    Rule(FILTER_LOGIC, OPENCYPHER, Logic.KLEENE),
    Rule(LIKE_CASE_SENSITIVE, SQL, True),
    Rule(MEMBERSHIP_ABSENT_LIST, COLLECTION, Truth.FALSE),
    Rule(BARE_PARAMETER_TYPED_BY, SQL, TypedBy.COLUMN),
    Rule(BARE_PARAMETER_TYPED_BY, OPENCYPHER, TypedBy.COLUMN),
    Rule(SUM_EMPTY, SQL, EmptyAggregate.NULL),
    Rule(SUM_EMPTY, SPARQL, EmptyAggregate.ZERO),
    Rule(SUM_EMPTY, OPENCYPHER, EmptyAggregate.NULL),
    Rule(SUM_ALL_NULL, SQL, EmptyAggregate.NULL),
    Rule(SUM_ALL_NULL, OPENCYPHER, EmptyAggregate.NULL),
    Rule(MAX_EMPTY, SQL, EmptyAggregate.NULL),
    Rule(MAX_EMPTY, SPARQL, EmptyAggregate.NULL),
    Rule(MAX_EMPTY, OPENCYPHER, EmptyAggregate.NULL),
    Rule(COUNT_EMPTY, SQL, EmptyAggregate.ZERO),
    Rule(COUNT_EMPTY, SPARQL, EmptyAggregate.ZERO),
    Rule(COUNT_EMPTY, OPENCYPHER, EmptyAggregate.ZERO),
    Rule(SKIPS_NULL, SQL, True),
    Rule(SKIPS_NULL, SPARQL, True),
    Rule(SKIPS_NULL, OPENCYPHER, True),
    Rule(UNGROUPED_EMPTY_ROWS, SQL, EmptyInputRows.ONE_ROW),
    Rule(UNGROUPED_EMPTY_ROWS, OPENCYPHER, EmptyInputRows.ONE_ROW),
    Rule(GROUPED_EMPTY_ROWS, SQL, EmptyInputRows.NO_ROW),
    Rule(GROUPED_EMPTY_ROWS, SPARQL, EmptyInputRows.NO_ROW),
    Rule(NULL_KEY_GROUPS, SQL, 1),
    Rule(NULL_KEY_GROUPS, OPENCYPHER, 1),
    Rule(AVG_INTEGER, SQL, AvgResult.DECIMAL_25_6_TRUNCATED),
    Rule(AVG_INTEGER, SPARQL, AvgResult.DOUBLE),
    Rule(MEDIAN_FUNCTION_INTEGER, SQL, MedianResult.ROUNDED_HALF_AWAY_FROM_ZERO),
    Rule(MEDIAN_FUNCTION_INTEGER, SPARQL, MedianResult.INTERPOLATED),
    Rule(PERCENTILE_CONT_INTEGER, SQL, MedianResult.INTERPOLATED),
    Rule(EDGE_BINDING, OPENCYPHER, EdgeBinding.EDGE_ISOMORPHISM),
    Rule(UNDIRECTED_SELF_LOOP_ROWS, OPENCYPHER, 1),
    Rule(PARALLEL_EDGE_ROWS, SPARQL, 2),
    Rule(PARALLEL_EDGE_ROWS, OPENCYPHER, 2),
    Rule(DISCONNECTED_PATTERNS, OPENCYPHER, Combination.CARTESIAN_PRODUCT),
    Rule(UNLABELLED_VERTEX_SPANS, OPENCYPHER, True),
    Rule(NODE_WITH_TWO_TYPES_ROWS, SPARQL, 1),
    Rule(VARIABLE_LENGTH_PATH_MODE, OPENCYPHER, PathMode.TRAIL),
    Rule(WRITE_ROW_COUNT, SQL, True),
    Rule(WRITE_ROW_COUNT, COLLECTION, True),
    Rule(JOINS_SQL_TRANSACTION, COLLECTION, True),
    Rule(JOINS_SQL_TRANSACTION, SPARQL, True),
    Rule(OWN_UNCOMMITTED_VISIBLE, OPENCYPHER, True),
)

# --- ask: a construct a subject refuses is not rendered ------------------------------------------
ASK_RULES = (
    Rule(accepts("match.second_clause"), OPENCYPHER, REFUSED),
    Rule(accepts("pattern.chained_edges"), OPENCYPHER, REFUSED),
    Rule(accepts("expression.is_not_null"), OPENCYPHER, REFUSED),
    Rule(accepts("where.boolean_attribute"), OPENCYPHER, REFUSED),
    Rule(accepts("count.boolean_attribute"), OPENCYPHER, REFUSED),
    Rule(accepts("compare.boolean_attributes"), OPENCYPHER, REFUSED),
    Rule(accepts("group_key.boolean_attribute"), OPENCYPHER, ACCEPTED),
    Rule(accepts("compare.bigint_attribute_with_float"), OPENCYPHER, REFUSED),
    Rule(accepts("parameters.cast"), OPENCYPHER, REFUSED),
    Rule(accepts("contains.parameter"), OPENCYPHER, REFUSED),
    Rule(accepts("skip.without_limit"), OPENCYPHER, REFUSED),
    Rule(accepts("aggregate.count_distinct"), OPENCYPHER, REFUSED),
    Rule(accepts("aggregate.avg"), OPENCYPHER, REFUSED),
    Rule(accepts("aggregate.max_min_sum.non_numeric"), OPENCYPHER, REFUSED),
    Rule(accepts("filter.bare_parameter"), COLLECTION, REFUSED),
    Rule(accepts("filter.in_list_parameter"), COLLECTION, REFUSED),
    Rule(accepts("like.parameter"), COLLECTION, REFUSED),
    Rule(accepts("query.boolean_constant"), COLLECTION, REFUSED),
    Rule(accepts("update.set_expression"), COLLECTION, REFUSED),
    Rule(accepts("update.set_unset_without_comma"), COLLECTION, REFUSED),
    Rule(accepts("aggregate.max_min.string_field"), COLLECTION, REFUSED),
    Rule(accepts("union_all.collection_queries"), COLLECTION, REFUSED),
    Rule(accepts("except.collection_queries"), COLLECTION, REFUSED),
    Rule(accepts("subquery.nested_unnest_view"), SQL_DOC, REFUSED),
    Rule(accepts("exists.unnest_element_view.positive_conjunct"), SQL_DOC, ACCEPTED),
    Rule(accepts("exists.unnest_element_view.under_or"), SQL_DOC, REFUSED),
    Rule(accepts("exists.unnest_element_view.under_not"), SQL_DOC, REFUSED),
    Rule(accepts("exists.unnest_element_view.in_semi_join"), SQL_DOC, REFUSED),
    Rule(accepts("exists.unnest_element_view.with_unnest_edge_views"), SQL_DOC, REFUSED),
    Rule(accepts("select.ungrouped_variable"), SPARQL, REFUSED),
    Rule(accepts("aggregate.median.without_group_by"), SPARQL, REFUSED),
    Rule(accepts("offset.without_limit"), SQL, REFUSED),
    Rule(accepts("identifier.start"), SQL, REFUSED),
    Rule(accepts("workspace.heterogeneous"), GRAPHSCRIPT, REFUSED),
    Rule(accepts("shortest_path.direction_parameter"), GRAPHSCRIPT, ACCEPTED),
    Rule(accepts("neighbors.direction_argument"), GRAPHSCRIPT, REFUSED),
    Rule(accepts("subgraph.vertex_outside.lookup"), GRAPHSCRIPT, REFUSED),
)

# --- equivalent: twin forms answer alike, so a mismatch between them is a finding ----------------
EQUIVALENT_RULES = (
    Rule(equivalent("hint_profile"), SQL, True),
    Rule(equivalent("hint_profile"), COLLECTION, True),
    Rule(equivalent("anonymous_block"), SQLSCRIPT, True),
    Rule(equivalent("set_operation_partition"), SQL, True),
    Rule(equivalent("set_operation_partition"), SQL_DOC, True),
    Rule(equivalent("set_operation_partition"), SPARQL, True),
    Rule(equivalent("not_kept_as_written"), COLLECTION, True),
    Rule(equivalent("from_graph_clause"), SPARQL, True),
    Rule(equivalent("workspace_no_cache"), OPENCYPHER, True),
    Rule(PEER_READ_COMMITTED, SQL, True),
    Rule(PEER_READ_COMMITTED, COLLECTION, True),
    Rule(PEER_READ_COMMITTED, SPARQL, True),
    Rule(PEER_READ_COMMITTED, OPENCYPHER, True),
)

# --- exact: where a value compares exactly ------------------------------------------------------
EXACT_RULES = (
    Rule(SUM_EXACT_BELOW, SQL, 2**63),
    Rule(CROSS_TYPE_EXACT_BELOW, SQL, 10**12),
    Rule(CROSS_TYPE_EXACT_BELOW, COLLECTION, 10**12),
    Rule(CROSS_TYPE_EXACT_BELOW, SPARQL, 10**12),
    Rule(CROSS_TYPE_EXACT_BELOW, OPENCYPHER, 10**12),
    Rule(exact("constant.double"), SQL, True),
    Rule(exact("constant.double"), OPENCYPHER, False),
    Rule(exact("constant.integer"), SQL, True),
    Rule(exact("constant.integer"), OPENCYPHER, True),
    Rule(exact("constant.string"), SQL, True),
    Rule(exact("constant.string"), OPENCYPHER, True),
    Rule(exact("constant.boolean"), SQL, True),
    Rule(exact("custom_key"), SQL, True),
    Rule(exact("custom_key"), COLLECTION, True),
    Rule(exact("custom_key"), SPARQL, True),
    Rule(exact("custom_key"), OPENCYPHER, True),
)

RULES = IR_RULES + COMPARE_RULES + ASK_RULES + EQUIVALENT_RULES + EXACT_RULES

DEFECTS = (
    Defect("A1", SPARQL),
    Defect("A2", SQL_DOC),
    Defect("A3", SPARQL),
    Defect("A4", SQL_DOC),
    Defect("A5", SQL_DOC),
    Defect("A6", SPARQL),
    Defect("A7", OPENCYPHER),
    Defect("A8", OPENCYPHER),
    Defect("A9", SQL_GRAPH),
    Defect("A10", SPARQL),
)

ERRORS = (
    ErrorClass(ErrorKind.CONNECTION_LOST, frozenset({-10709, -10807, -10108, -10821, -10806})),
    ErrorClass(ErrorKind.TIMEOUT, frozenset({613})),
    ErrorClass(ErrorKind.TIMEOUT, marks=("timeout", "abort"), all_marks=True, ignore_case=True),
    ErrorClass(ErrorKind.REJECTION, frozenset({129}), ("generic. Error -", "Unsupported functionality", "supports only")),
    ErrorClass(ErrorKind.INTERNAL, frozenset({2, 3, 129, 2048})),
    ErrorClass(ErrorKind.INTERNAL, marks=("internal error", "assertion", "must not reach here"), ignore_case=True),
    ErrorClass(ErrorKind.REJECTION),
)
