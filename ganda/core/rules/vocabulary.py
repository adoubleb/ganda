"""The questions every engine's catalog answers, their answer domains, and the core IR's
own answers to them (`IR_ANSWERS`): where a language's answer differs from the IR's, a
native rendering cannot ask that shape as the IR means it."""
from __future__ import annotations

import enum

from ganda.core.rules.model import IR, Answer, Basis, Question, Verdict  # noqa: F401 -- Verdict for the catalogs


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


class NumericKind(enum.StrEnum):
    INTEGER = "integer"
    DECIMAL = "decimal"
    DOUBLE = "double"


# --- ordering ------------------------------------------------------------------------------------
NULLS_ASC = Question("order.nulls.asc", NullPlacement)
NULLS_DESC = Question("order.nulls.desc", NullPlacement)
STRING_ORDER = Question("order.string", StringOrder)

# --- values --------------------------------------------------------------------------------------
INTEGER_RANGE = Question("value.integer.range", tuple)
STRING_MAX_LENGTH = Question("value.string.max_length", int)
SIGNED_ZERO_KEPT = Question("value.float.signed_zero_kept", bool)
STRING_PADDING = Question("value.string.padding", Padding)
EMPTY_STRING_IS_NULL = Question("value.string.empty_is_null", bool)
ABSENT_IS_NULL = Question("value.absent_is_null", bool)

# --- predicates ----------------------------------------------------------------------------------
FILTER_LOGIC = Question("filter.logic", Logic)
LIKE_CASE_SENSITIVE = Question("filter.like.case_sensitive", bool)

# --- aggregates ----------------------------------------------------------------------------------
SUM_EMPTY = Question("aggregate.sum.empty_input", EmptyAggregate)
MAX_EMPTY = Question("aggregate.max.empty_input", EmptyAggregate)
COUNT_EMPTY = Question("aggregate.count.empty_input", EmptyAggregate)
UNGROUPED_EMPTY_ROWS = Question("aggregate.ungrouped.empty_input.rows", EmptyInputRows)
GROUPED_EMPTY_ROWS = Question("aggregate.grouped.empty_input.rows", EmptyInputRows)

# --- arithmetic ----------------------------------------------------------------------------------
INTEGER_DIVISION = Question("arithmetic.integer_division.result", NumericKind)

# --- pattern matching ----------------------------------------------------------------------------
EDGE_BINDING = Question("match.edge_binding", EdgeBinding)
UNDIRECTED_SELF_LOOP_ROWS = Question("match.undirected.self_loop.rows", int)
VARIABLE_LENGTH_PATH_MODE = Question("path.variable_length.mode", PathMode)

# --- transactions --------------------------------------------------------------------------------
PEER_SEES_UNCOMMITTED = Question("transaction.peer_sees_uncommitted", bool)
OWN_UNCOMMITTED_VISIBLE = Question("transaction.own_uncommitted_visible", bool)


IR_ANSWERS = (
    Answer(EDGE_BINDING, IR, EdgeBinding.HOMOMORPHISM, Basis.DEFINITION),
    Answer(VARIABLE_LENGTH_PATH_MODE, IR, PathMode.WALK, Basis.DEFINITION),
    Answer(UNDIRECTED_SELF_LOOP_ROWS, IR, 2, Basis.DEFINITION),
    Answer(FILTER_LOGIC, IR, Logic.KLEENE, Basis.DEFINITION),
)
