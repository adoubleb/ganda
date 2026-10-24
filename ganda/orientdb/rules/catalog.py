"""OrientDB's engine rules (`ganda.core.rules`): what the fuzzer acts on so that it asks
and compares only what the engine's execution paths answer alike. Its three data models
speak one language, OrientDB SQL, so a subject is an execution path: a scan, the same
statement served by an index, MATCH, TRAVERSE, the script door (the transaction door
too) and the /batch door. The probe checks that check the rules are in
`checks.py`."""
from __future__ import annotations

import enum

from ganda.core.rules.model import Defect, Kind, Question, Rule, Verdict
from ganda.core.rules.vocabulary import (
    FILTER_LOGIC,
    LIKE_CASE_SENSITIVE,
    OWN_UNCOMMITTED_VISIBLE,
    PARALLEL_EDGE_ROWS,
    STRING_ORDER,
    SUM_EXACT_BELOW,
    UNDIRECTED_SELF_LOOP_ROWS,
    UNGROUPED_EMPTY_ROWS,
    WRITE_ROW_COUNT,
    EmptyInputRows,
    Logic,
    StringOrder,
)


class Subject(enum.StrEnum):
    SQL = "osql"
    SQL_INDEX = "osql@index"
    MATCH = "match"
    TRAVERSE = "traverse"
    SCRIPT = "script"
    BATCH = "batch"


class Strategy(enum.StrEnum):
    DEPTH_FIRST = "depth-first"
    BREADTH_FIRST = "breadth-first"


SQL, SQL_INDEX, MATCH, TRAVERSE, SCRIPT, BATCH = Subject
COMPARE, ASK, EQUIVALENT, EXACT = Kind.COMPARE, Kind.ASK, Kind.EQUIVALENT, Kind.EXACT
REFUSED, ACCEPTED = Verdict.REFUSED, Verdict.ACCEPTED


def accepts(construct: str) -> Question:
    return Question(f"accepts.{construct}", Verdict, ASK)


def equivalent(form: str) -> Question:
    return Question(f"equivalent.{form}", bool, EQUIVALENT)


def exact(value: str, domain=bool) -> Question:
    return Question(f"exact.{value}", domain, EXACT)


ORDER_BY_STRING = Question("order.string.order_by", StringOrder, COMPARE)
NEGATIVE_ZERO_BELOW_ZERO = Question("compare.negative_zero_below_zero", bool, COMPARE)
LIKE_ONE_CHARACTER = Question("filter.like.one_character_wildcard", str, COMPARE)
CI_OPERATORS = Question("filter.case_insensitive.operators", str, COMPARE)
TRAVERSE_DEFAULT = Question("traverse.default_strategy", Strategy, COMPARE)
OPTIONAL_NO_PATH = Question("match.optional.no_path.rows", EmptyInputRows, COMPARE)
ABSENT_SURVIVES_PROJECTION = Question("projection.keeps_absent_field", bool, COMPARE)
EXPAND_WRAPS_EMBEDDED = Question("expand.embedded.wrapped_under_value", bool, COMPARE)
UNIQUE_DEFAULT_IGNORES_NULLS = Question("index.unique.default_ignores_nulls", bool, COMPARE)
DELETE_VERTEX_CASCADES = Question("delete_vertex.cascades_edges", bool, COMPARE)
SHORTEST_PATH_UNREACHABLE = Question("shortest_path.unreachable.result", int, COMPARE)
SHORTEST_PATH_SAME_RECORD = Question("shortest_path.same_record.result", int, COMPARE)
UPSERT_OTHER_PREDICATE_KEEPS_ID = Question("upsert.other_predicate.keeps_id", bool, COMPARE)
DELETE_FROM_DOC_KEEPS_LINKS = Question("delete.document.keeps_incoming_links", bool, COMPARE)
STATEMENT_ATOMIC = Question("statement.atomic", bool, COMPARE)
INT32_SUM_EXACT_BELOW = exact("sum.int32.below", int)

# --- compare: paths answering differently are not compared where the question is exercised ----
COMPARE_RULES = (
    Rule(STRING_ORDER, SQL, StringOrder.UTF16_CODE_UNIT),
    Rule(ORDER_BY_STRING, SQL, StringOrder.COLLATION),
    Rule(ORDER_BY_STRING, SQL_INDEX, StringOrder.UTF16_CODE_UNIT),
    Rule(NEGATIVE_ZERO_BELOW_ZERO, SQL, True),
    Rule(FILTER_LOGIC, SQL, Logic.TWO_VALUED),
    Rule(LIKE_CASE_SENSITIVE, SQL, False),
    Rule(LIKE_ONE_CHARACTER, SQL, "?"),
    Rule(CI_OPERATORS, SQL, "= <> < <= > >="),
    Rule(UNGROUPED_EMPTY_ROWS, SQL, EmptyInputRows.NO_ROW),
    Rule(UNDIRECTED_SELF_LOOP_ROWS, SQL, 2),
    Rule(PARALLEL_EDGE_ROWS, SQL, 2),
    Rule(PARALLEL_EDGE_ROWS, MATCH, 2),
    Rule(TRAVERSE_DEFAULT, TRAVERSE, Strategy.DEPTH_FIRST),
    Rule(OPTIONAL_NO_PATH, MATCH, EmptyInputRows.ONE_ROW),
    Rule(ABSENT_SURVIVES_PROJECTION, SQL, False),
    Rule(EXPAND_WRAPS_EMBEDDED, SQL, True),
    Rule(UNIQUE_DEFAULT_IGNORES_NULLS, SQL_INDEX, False),
    Rule(DELETE_VERTEX_CASCADES, SQL, True),
    Rule(SHORTEST_PATH_UNREACHABLE, SQL, 0),
    Rule(SHORTEST_PATH_SAME_RECORD, SQL, 1),
    Rule(UPSERT_OTHER_PREDICATE_KEEPS_ID, SQL, False),
    Rule(DELETE_FROM_DOC_KEEPS_LINKS, SQL, True),
    Rule(STATEMENT_ATOMIC, SQL, False),
    Rule(WRITE_ROW_COUNT, SQL, True),
    Rule(OWN_UNCOMMITTED_VISIBLE, SCRIPT, True),
)

# --- ask: a construct a path refuses is not rendered --------------------------------------------
ASK_RULES = (
    Rule(accepts("projection.comparison"), SQL, REFUSED),
    Rule(accepts("let.after_where"), SQL, REFUSED),
    Rule(accepts("update.increment"), SQL, REFUSED),
    Rule(accepts("update.content.parameter"), SQL, REFUSED),
    Rule(accepts("merge.parameter"), SQL, REFUSED),
    Rule(accepts("update.filtered_list_element"), SQL, REFUSED),
    Rule(accepts("delete.timeout"), SQL, REFUSED),
    Rule(accepts("delete_from.vertex_or_edge_class"), SQL, REFUSED),
    Rule(accepts("instanceof.parameter"), SQL, REFUSED),
    Rule(accepts("match.timeout"), MATCH, REFUSED),
    Rule(accepts("max_depth.parameter"), MATCH, REFUSED),
    Rule(accepts("max_depth.parameter"), TRAVERSE, REFUSED),
    Rule(accepts("ddl.in_transaction"), SCRIPT, REFUSED),
    Rule(accepts("named_parameter"), BATCH, REFUSED),
    Rule(accepts("map_parameter"), BATCH, ACCEPTED),
)

# --- equivalent: twin forms answer alike, so a mismatch between them is a finding ----------------
EQUIVALENT_RULES = (
    Rule(equivalent("transaction_door"), SCRIPT, True),
)

# --- exact: where a value compares exactly ------------------------------------------------------
EXACT_RULES = (
    Rule(SUM_EXACT_BELOW, SQL, 2**63),
    Rule(INT32_SUM_EXACT_BELOW, SQL, 2**31),
)

# no IR definition: the code compares no subject's answer with the core IR's
RULES = COMPARE_RULES + ASK_RULES + EQUIVALENT_RULES + EXACT_RULES

DEFECTS = (
    Defect("O1", SQL_INDEX),
    Defect("O2", SQL_INDEX),
    Defect("O5", SQL),
    Defect("O6", SQL_INDEX),
    Defect("O7", SQL),
    Defect("O8", SQL),
    Defect("O9", SQL),
    Defect("O10", SQL_INDEX),
    Defect("O11", SQL_INDEX),
    Defect("O12", SQL),
    Defect("O13", SQL),
    Defect("O14", SQL_INDEX),
    Defect("O15", SQL),
    Defect("O16", SQL_INDEX),
    Defect("O17", SQL),
    Defect("O18", SQL_INDEX),
    Defect("O19", SQL),
    Defect("O20", SQL),
    Defect("O21", SQL),
    Defect("O22", MATCH),
    Defect("O23", SQL_INDEX),
    Defect("O24", SQL),
    Defect("O25", SQL),
    Defect("O26", SQL),
    Defect("O27", SQL),
    Defect("O28", SQL),
    Defect("O29", SQL),
    Defect("O30", SQL),
    Defect("O31", SCRIPT),
    Defect("O32", SQL),
    Defect("O33", SQL),
    Defect("O34", SQL),
    Defect("O35", SQL),
    Defect("O36", SQL_INDEX),
    Defect("O37", SQL_INDEX),
    Defect("O38", SCRIPT),
    Defect("O39", SQL),
    Defect("O40", SQL),
    Defect("O41", SQL),
    Defect("O42", SQL),
    Defect("O43", SQL),
    Defect("O44", TRAVERSE),
    Defect("O45", MATCH),
    Defect("O46", SQL),
)

ERRORS = ()
