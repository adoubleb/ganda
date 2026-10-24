"""The questions every engine's catalog may answer, their answer domains, and the core
IR's own answers (`IR_RULES`): where a subject answers a compare question differently
from the IR, its native rendering cannot ask that shape as the IR means it."""
from __future__ import annotations

import enum

from ganda.core.rules.model import IR, Kind, Question, Rule

COMPARE, EXACT, EQUIVALENT = Kind.COMPARE, Kind.EXACT, Kind.EQUIVALENT


class NullPlacement(enum.StrEnum):
    FIRST = "first"
    LAST = "last"


class StringOrder(enum.StrEnum):
    UTF16_CODE_UNIT = "utf-16-code-unit"
    CODE_POINT = "code-point"
    COLLATION = "collation"


class Padding(enum.StrEnum):
    NO_PAD = "no-pad"
    PAD_SPACE = "pad-space"


class Logic(enum.StrEnum):
    KLEENE = "kleene"
    TWO_VALUED = "two-valued"


class Truth(enum.StrEnum):
    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


class EmptyAggregate(enum.StrEnum):
    NULL = "null"
    ZERO = "zero"


class EmptyInputRows(enum.StrEnum):
    ONE_ROW = "one-row"
    NO_ROW = "no-row"


class EdgeBinding(enum.StrEnum):
    HOMOMORPHISM = "homomorphism"
    EDGE_ISOMORPHISM = "edge-isomorphism"


class PathMode(enum.StrEnum):
    WALK = "walk"
    TRAIL = "trail"
    ACYCLIC = "acyclic"
    SIMPLE = "simple"


class Combination(enum.StrEnum):
    CARTESIAN_PRODUCT = "cartesian-product"


class TypedBy(enum.StrEnum):
    VALUE = "value"
    COLUMN = "column"


# --- order and values ----------------------------------------------------------------------------
NULLS_ASC = Question("order.nulls.asc", NullPlacement, COMPARE)
NULLS_DESC = Question("order.nulls.desc", NullPlacement, COMPARE)
STRING_ORDER = Question("order.string", StringOrder, COMPARE)
ABSENT_IS_NULL = Question("value.absent_is_null", bool, COMPARE)
EMPTY_STRING_IS_NULL = Question("value.string.empty_is_null", bool, COMPARE)
STRING_PADDING = Question("value.string.padding", Padding, COMPARE)
DOUBLE_EXACT_ROUND_TRIP = Question("value.double.exact_round_trip", bool, COMPARE)

# --- predicates ----------------------------------------------------------------------------------
FILTER_LOGIC = Question("filter.logic", Logic, COMPARE)
LIKE_CASE_SENSITIVE = Question("filter.like.case_sensitive", bool, COMPARE)
MEMBERSHIP_ABSENT_LIST = Question("filter.membership.absent_list", Truth, COMPARE)
BARE_PARAMETER_TYPED_BY = Question("parameter.bare.typed_by", TypedBy, COMPARE)

# --- aggregates ----------------------------------------------------------------------------------
SUM_EMPTY = Question("aggregate.sum.empty_input", EmptyAggregate, COMPARE)
SUM_ALL_NULL = Question("aggregate.sum.all_null_input", EmptyAggregate, COMPARE)
MAX_EMPTY = Question("aggregate.max.empty_input", EmptyAggregate, COMPARE)
COUNT_EMPTY = Question("aggregate.count.empty_input", EmptyAggregate, COMPARE)
SKIPS_NULL = Question("aggregate.skips_null", bool, COMPARE)
UNGROUPED_EMPTY_ROWS = Question("aggregate.ungrouped.empty_input.rows", EmptyInputRows, COMPARE)
GROUPED_EMPTY_ROWS = Question("aggregate.grouped.empty_input.rows", EmptyInputRows, COMPARE)
NULL_KEY_GROUPS = Question("aggregate.group.null_key.groups", int, COMPARE)
SUM_EXACT_BELOW = Question("aggregate.sum.integer.exact_below", int, EXACT)

# --- pattern matching ----------------------------------------------------------------------------
EDGE_BINDING = Question("match.edge_binding", EdgeBinding, COMPARE)
UNDIRECTED_SELF_LOOP_ROWS = Question("match.undirected.self_loop.rows", int, COMPARE)
PARALLEL_EDGE_ROWS = Question("match.parallel_edges.rows", int, COMPARE)
DISCONNECTED_PATTERNS = Question("match.disconnected_patterns", Combination, COMPARE)
VARIABLE_LENGTH_PATH_MODE = Question("path.variable_length.mode", PathMode, COMPARE)

# --- writes and transactions ---------------------------------------------------------------------
WRITE_ROW_COUNT = Question("statement.write.answers_row_count", bool, COMPARE)
JOINS_SQL_TRANSACTION = Question("transaction.joins_sql_transaction", bool, COMPARE)
OWN_UNCOMMITTED_VISIBLE = Question("transaction.own_uncommitted_visible", bool, COMPARE)
PEER_READ_COMMITTED = Question("equivalent.peer_read.committed_state", bool, EQUIVALENT)


IR_RULES = (
    Rule(FILTER_LOGIC, IR, Logic.KLEENE),
    Rule(MEMBERSHIP_ABSENT_LIST, IR, Truth.FALSE),
    Rule(BARE_PARAMETER_TYPED_BY, IR, TypedBy.VALUE),
    Rule(NULL_KEY_GROUPS, IR, 1),
    Rule(EDGE_BINDING, IR, EdgeBinding.HOMOMORPHISM),
    Rule(UNDIRECTED_SELF_LOOP_ROWS, IR, 2),
    Rule(PARALLEL_EDGE_ROWS, IR, 2),
    Rule(DISCONNECTED_PATTERNS, IR, Combination.CARTESIAN_PRODUCT),
    Rule(VARIABLE_LENGTH_PATH_MODE, IR, PathMode.WALK),
    Rule(JOINS_SQL_TRANSACTION, IR, True),
    Rule(OWN_UNCOMMITTED_VISIBLE, IR, True),
)
