"""SAP HANA's engine rules (`ganda.core.rules`): what the fuzzer acts on so that it asks
and compares only what HANA's languages answer alike. The probe facts that check them
are in `checks.py`."""
from __future__ import annotations

import enum

from ganda.core.rules.model import Defect, ErrorClass, ErrorKind, Kind, Question, Rule, Verdict
from ganda.core.rules.vocabulary import (
    EDGE_BINDING,
    FILTER_LOGIC,
    IR_RULES,
    JOINS_SQL_TRANSACTION,
    MAX_EMPTY,
    MEMBERSHIP_ABSENT_LIST,
    NULLS_ASC,
    NULLS_DESC,
    OWN_UNCOMMITTED_VISIBLE,
    PEER_READ_COMMITTED,
    STRING_ORDER,
    SUM_ALL_NULL,
    SUM_EXACT_BELOW,
    UNDIRECTED_SELF_LOOP_ROWS,
    UNGROUPED_EMPTY_ROWS,
    VARIABLE_LENGTH_PATH_MODE,
    WRITE_ROW_COUNT,
    EdgeBinding,
    EmptyAggregate,
    EmptyInputRows,
    Logic,
    NullPlacement,
    PathMode,
    StringOrder,
    Truth,
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
    Rule(NULLS_ASC, OPENCYPHER, NullPlacement.LAST),
    Rule(NULLS_DESC, SQL, NullPlacement.LAST),
    Rule(NULLS_DESC, OPENCYPHER, NullPlacement.FIRST),
    Rule(STRING_ORDER, SQL, StringOrder.UTF16_CODE_UNIT),
    Rule(STRING_ORDER, OPENCYPHER, StringOrder.UTF16_CODE_UNIT),
    Rule(FILTER_LOGIC, COLLECTION, Logic.KLEENE),
    Rule(FILTER_LOGIC, SPARQL, Logic.KLEENE),
    Rule(MEMBERSHIP_ABSENT_LIST, COLLECTION, Truth.FALSE),
    Rule(SUM_ALL_NULL, OPENCYPHER, EmptyAggregate.NULL),
    Rule(MAX_EMPTY, SPARQL, EmptyAggregate.NULL),
    Rule(UNGROUPED_EMPTY_ROWS, SQL, EmptyInputRows.ONE_ROW),
    Rule(UNGROUPED_EMPTY_ROWS, OPENCYPHER, EmptyInputRows.ONE_ROW),
    Rule(AVG_INTEGER, SQL, AvgResult.DECIMAL_25_6_TRUNCATED),
    Rule(AVG_INTEGER, SPARQL, AvgResult.DOUBLE),
    Rule(MEDIAN_FUNCTION_INTEGER, SQL, MedianResult.ROUNDED_HALF_AWAY_FROM_ZERO),
    Rule(PERCENTILE_CONT_INTEGER, SQL, MedianResult.INTERPOLATED),
    Rule(EDGE_BINDING, OPENCYPHER, EdgeBinding.EDGE_ISOMORPHISM),
    Rule(UNDIRECTED_SELF_LOOP_ROWS, OPENCYPHER, 1),
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
    Rule(accepts("expression.is_not_null"), OPENCYPHER, REFUSED),
    Rule(accepts("where.boolean_attribute"), OPENCYPHER, REFUSED),
    Rule(accepts("count.boolean_attribute"), OPENCYPHER, REFUSED),
    Rule(accepts("compare.boolean_attributes"), OPENCYPHER, REFUSED),
    Rule(accepts("compare.bigint_attribute_with_float"), OPENCYPHER, REFUSED),
    Rule(accepts("parameters.cast"), OPENCYPHER, REFUSED),
    Rule(accepts("contains.parameter"), OPENCYPHER, REFUSED),
    Rule(accepts("skip.without_limit"), OPENCYPHER, REFUSED),
    Rule(accepts("aggregate.count_distinct"), OPENCYPHER, REFUSED),
    Rule(accepts("aggregate.avg"), OPENCYPHER, REFUSED),
    Rule(accepts("aggregate.median"), OPENCYPHER, REFUSED),
    Rule(accepts("path.variable_length"), OPENCYPHER, ACCEPTED),
    Rule(accepts("opencypher_table.in_correlated_scalar_subquery"), SQL_GRAPH, ACCEPTED),
    Rule(accepts("opencypher_table.in_in_subquery"), SQL_GRAPH, ACCEPTED),
    Rule(accepts("opencypher_table.in_not_in_subquery"), SQL_GRAPH, ACCEPTED),
    Rule(accepts("aggregate.max_min_sum.non_numeric"), OPENCYPHER, REFUSED),
    Rule(accepts("filter.bare_parameter"), COLLECTION, REFUSED),
    Rule(accepts("filter.in_list_parameter"), COLLECTION, REFUSED),
    Rule(accepts("like.parameter"), COLLECTION, REFUSED),
    Rule(accepts("query.boolean_constant"), COLLECTION, REFUSED),
    Rule(accepts("update.set_expression"), COLLECTION, REFUSED),
    Rule(accepts("update.set_unset_without_comma"), COLLECTION, REFUSED),
    Rule(accepts("aggregate.max_min.string_field"), COLLECTION, REFUSED),
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

_SUBJECT_RULES = COMPARE_RULES + ASK_RULES + EQUIVALENT_RULES + EXACT_RULES
# the core IR's definitions of the compare questions this engine answers
RULES = tuple(r for r in IR_RULES if r.question in {s.question for s in _SUBJECT_RULES}) + _SUBJECT_RULES

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
