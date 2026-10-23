"""SAP HANA's engine rules (`ganda.core.rules`)."""
from __future__ import annotations

import enum

from ganda.core.rules.model import Answer, Basis, Campaign, Catalog, Defect, Effect, ErrorClass, ErrorKind, Finding, Handling, Question, Standard, VendorDoc, Verdict
from ganda.core.rules.vocabulary import (
    ABSENT_IS_NULL,
    COUNT_EMPTY,
    EDGE_BINDING,
    EMPTY_STRING_IS_NULL,
    FILTER_LOGIC,
    GROUPED_EMPTY_ROWS,
    INTEGER_DIVISION,
    INTEGER_RANGE,
    IR_ANSWERS,
    LIKE_CASE_SENSITIVE,
    MAX_EMPTY,
    NULLS_ASC,
    NULLS_DESC,
    OWN_UNCOMMITTED_VISIBLE,
    PEER_SEES_UNCOMMITTED,
    SIGNED_ZERO_KEPT,
    STRING_MAX_LENGTH,
    STRING_ORDER,
    STRING_PADDING,
    SUM_EMPTY,
    UNDIRECTED_SELF_LOOP_ROWS,
    UNGROUPED_EMPTY_ROWS,
    VARIABLE_LENGTH_PATH_MODE,
    EdgeBinding,
    EmptyAggregate,
    EmptyInputRows,
    Logic,
    NullPlacement,
    NumericKind,
    Padding,
    PathMode,
    StringOrder,
    Truth,
)


class Language(enum.StrEnum):
    SQL = "sql"
    COLLECTION = "collection"
    SPARQL = "sparql"
    OPENCYPHER = "opencypher"
    SQLSCRIPT = "sqlscript"


class StoredAs(enum.StrEnum):
    BIGINT = "bigint"
    DECIMAL = "decimal"
    XSD_LONG = "xsd:long"
    XSD_DOUBLE = "xsd:double"
    XSD_BOOLEAN = "xsd:boolean"


class AvgResult(enum.StrEnum):
    DECIMAL_25_6_TRUNCATED = "decimal(25,6)-truncated"
    DOUBLE = "double"
    XSD_DECIMAL = "xsd:decimal"


class MedianResult(enum.StrEnum):
    INTERPOLATED = "interpolated"
    ROUNDED_HALF_AWAY_FROM_ZERO = "rounded-half-away-from-zero"


class Dataset(enum.StrEnum):
    ALL_GRAPHS = "all-graphs"
    DEFAULT_GRAPH = "default-graph"


SQL, COLLECTION, SPARQL, OPENCYPHER, SQLSCRIPT = Language

SQL_REF = VendorDoc("SAP HANA SQL Reference", "QRC 3/2026")
HINTS = VendorDoc("SAP HANA SQL Reference", "QRC 3/2026", "HINT Details")
DOC_STORE = VendorDoc("SAP HANA JSON Document Store Guide", "QRC 3/2026")
SPARQL_REF = VendorDoc("SAP HANA SPARQL Reference", "QRC 3/2026")
KG_GUIDE = VendorDoc("SAP HANA Knowledge Graph Guide", "QRC 3/2026")
PGE_REF = VendorDoc("SAP HANA Property Graph Engine Reference", "QRC 2/2026")
SPARQL11 = "SPARQL 1.1 Query Language"

VERIFICATION = Campaign("20261009-095838")
H3_CAMPAIGN = Campaign("20261009-165805")
TWELVE_HOURS = Campaign("camp12h")

ORDER_TYPES = Question("order.types", tuple)
STORED_INTEGER = Question("value.integer.stored_as", StoredAs)
STORED_FRACTION = Question("value.fraction.stored_as", StoredAs)
STORED_DOUBLE = Question("value.double.stored_as", StoredAs)
STORED_BOOLEAN = Question("value.boolean.stored_as", StoredAs)
INTEGER_FRACTION_ONE_TYPE = Question("compare.integer_and_fraction.one_type", bool)
IMPLICIT_CAST = Question("compare.implicit_cast", bool)
CROSS_TYPE_EXACT = Question("compare.cross_type_numeric.exact", bool)
FOR_ANY_ABSENT = Question("filter.for_any.absent_array", Truth)
AVG_INTEGER = Question("aggregate.avg.integer.result", AvgResult)
AVG_INTEGER_DECIMAL_CAST = Question("aggregate.avg.integer.decimal_cast.result", AvgResult)
MEDIAN_INTEGER = Question("aggregate.median.integer.result", MedianResult)
PERCENTILE_CONT_INTEGER = Question("aggregate.percentile_cont_half.integer.result", MedianResult)
DEFAULT_DATASET = Question("query.default_dataset", Dataset)
NAMES_CASE_SENSITIVE = Question("query.names.case_sensitive", bool)
JOINS_SQL_TRANSACTION = Question("transaction.joins_sql_transaction", bool)

HINT_PROFILE_EQUIVALENT = Question("equivalent.hint_profile", bool)
ANONYMOUS_BLOCK_EQUIVALENT = Question("equivalent.anonymous_block", bool)
SET_PARTITION_EQUIVALENT = Question("equivalent.set_operation_partition", bool)
NOT_AS_WRITTEN_EQUIVALENT = Question("equivalent.not_kept_as_written", bool)
FROM_CLAUSE_EQUIVALENT = Question("equivalent.from_graph_clause", bool)


def accepts(construct: str) -> Question:
    return Question(f"accepts.{construct}", Verdict)


REFUSED, ACCEPTED = Verdict.REFUSED, Verdict.ACCEPTED
OBSERVED, ASSUMED, VENDOR_DOC = Basis.OBSERVED, Basis.ASSUMED, Basis.VENDOR_DOC
GRAPH_ROW = ("G8", "G8b", "G9", "G22")
INT64 = (-(2**63), 2**63 - 1)
HINT_FACTS = tuple(f"P{n}" for n in range(1, 18))

ANSWERS = IR_ANSWERS + (
    # --- order ---
    Answer(NULLS_ASC, SQL, NullPlacement.FIRST, ASSUMED, ("R5",)),
    Answer(NULLS_DESC, SQL, NullPlacement.LAST, ASSUMED, ("R5",)),
    Answer(NULLS_ASC, COLLECTION, NullPlacement.FIRST, VENDOR_DOC, ("D12",), (DOC_STORE,)),
    Answer(NULLS_DESC, COLLECTION, NullPlacement.LAST, VENDOR_DOC, ("D12",), (DOC_STORE,)),
    Answer(NULLS_ASC, SPARQL, NullPlacement.FIRST, ASSUMED, ("K17",), (Standard(SPARQL11, "15.1"),)),
    Answer(NULLS_DESC, SPARQL, NullPlacement.LAST, ASSUMED, (), (Standard(SPARQL11, "15.1"),)),
    Answer(NULLS_ASC, OPENCYPHER, NullPlacement.LAST, OBSERVED, ("G5",)),
    Answer(NULLS_DESC, OPENCYPHER, NullPlacement.FIRST, OBSERVED, ("G5", "G5b")),
    Answer(STRING_ORDER, SQL, StringOrder.UTF16_CODE_UNIT, ASSUMED, ("R3",)),
    Answer(STRING_ORDER, COLLECTION, StringOrder.UTF16_CODE_UNIT, OBSERVED, ("D22",)),
    Answer(STRING_ORDER, SPARQL, StringOrder.CODE_POINT, ASSUMED, ("K17",), (Standard(SPARQL11, "15.1"),)),
    Answer(STRING_ORDER, OPENCYPHER, StringOrder.UTF16_CODE_UNIT, OBSERVED, ("G6",)),
    Answer(ORDER_TYPES, COLLECTION, ("null", "number", "string", "object", "boolean", "array"), VENDOR_DOC, ("D12",), (DOC_STORE,)),
    # --- values ---
    Answer(INTEGER_RANGE, SQL, INT64, VENDOR_DOC, ("R0", "R1"), (SQL_REF,)),
    Answer(INTEGER_RANGE, COLLECTION, INT64, VENDOR_DOC, ("D1", "D4"), (DOC_STORE,)),
    Answer(INTEGER_RANGE, SPARQL, INT64, VENDOR_DOC, ("K2",), (SPARQL_REF,)),
    Answer(INTEGER_RANGE, OPENCYPHER, INT64, OBSERVED, ("G11",)),
    Answer(STRING_MAX_LENGTH, SQL, 5000, VENDOR_DOC, (), (SQL_REF,)),
    Answer(SIGNED_ZERO_KEPT, SQL, False, VENDOR_DOC, ("R1",), (SQL_REF,)),
    Answer(STRING_PADDING, SQL, Padding.NO_PAD, OBSERVED, ("R3",)),
    Answer(EMPTY_STRING_IS_NULL, SQL, False, OBSERVED, ("R4",)),
    Answer(ABSENT_IS_NULL, SQL, True, ASSUMED),
    Answer(ABSENT_IS_NULL, COLLECTION, True, VENDOR_DOC, ("D7",), (DOC_STORE,)),
    Answer(ABSENT_IS_NULL, SPARQL, True, ASSUMED),
    Answer(STORED_INTEGER, COLLECTION, StoredAs.BIGINT, VENDOR_DOC, ("D1",), (DOC_STORE,)),
    Answer(STORED_FRACTION, COLLECTION, StoredAs.DECIMAL, VENDOR_DOC, ("D1", "D5"), (DOC_STORE,)),
    Answer(STORED_INTEGER, SPARQL, StoredAs.XSD_LONG, VENDOR_DOC, ("K1", "K2"), (SPARQL_REF,)),
    Answer(STORED_DOUBLE, SPARQL, StoredAs.XSD_DOUBLE, VENDOR_DOC, ("K1",), (SPARQL_REF,)),
    Answer(STORED_BOOLEAN, SPARQL, StoredAs.XSD_BOOLEAN, VENDOR_DOC, ("K1",), (SPARQL_REF,)),
    # --- comparison ---
    Answer(INTEGER_FRACTION_ONE_TYPE, COLLECTION, True, VENDOR_DOC, ("D4", "D5"), (DOC_STORE,)),
    Answer(IMPLICIT_CAST, COLLECTION, False, VENDOR_DOC, ("D1", "D4", "D5"), (DOC_STORE,)),
    Answer(CROSS_TYPE_EXACT, SQL, True, OBSERVED, ("R21",)),
    Answer(CROSS_TYPE_EXACT, COLLECTION, True, OBSERVED, ("D24",)),
    Answer(CROSS_TYPE_EXACT, SPARQL, True, OBSERVED, ("K24",)),
    Answer(CROSS_TYPE_EXACT, OPENCYPHER, True, OBSERVED, ("G36",)),
    # --- filter ---
    Answer(FILTER_LOGIC, SQL, Logic.KLEENE, ASSUMED),
    Answer(FILTER_LOGIC, COLLECTION, Logic.KLEENE, OBSERVED, ("D19",)),
    Answer(FILTER_LOGIC, SPARQL, Logic.KLEENE, ASSUMED, ("K5",), (Standard(SPARQL11, "17.2"),)),
    Answer(FILTER_LOGIC, OPENCYPHER, Logic.KLEENE, OBSERVED, ("G3",)),
    Answer(LIKE_CASE_SENSITIVE, SQL, True, ASSUMED, ("R10",)),
    Answer(FOR_ANY_ABSENT, COLLECTION, Truth.FALSE, ASSUMED, ("D8",)),
    # --- aggregate ---
    Answer(SUM_EMPTY, SQL, EmptyAggregate.NULL, ASSUMED),
    Answer(SUM_EMPTY, SPARQL, EmptyAggregate.ZERO, ASSUMED, ("K8",), (Standard(SPARQL11, "18.5.1"),)),
    Answer(SUM_EMPTY, OPENCYPHER, EmptyAggregate.NULL, OBSERVED, ("G4",), (PGE_REF,), documented=EmptyAggregate.ZERO),
    Answer(MAX_EMPTY, SQL, EmptyAggregate.NULL, ASSUMED),
    Answer(MAX_EMPTY, SPARQL, EmptyAggregate.NULL, ASSUMED, ("K7",), (Standard(SPARQL11, "18.5.1"),)),
    Answer(MAX_EMPTY, OPENCYPHER, EmptyAggregate.NULL, OBSERVED, ("G4",)),
    Answer(COUNT_EMPTY, SQL, EmptyAggregate.ZERO, ASSUMED),
    Answer(COUNT_EMPTY, SPARQL, EmptyAggregate.ZERO, ASSUMED, ("K7",), (Standard(SPARQL11, "18.5.1"),)),
    Answer(COUNT_EMPTY, OPENCYPHER, EmptyAggregate.ZERO, OBSERVED, ("G4",)),
    Answer(UNGROUPED_EMPTY_ROWS, SQL, EmptyInputRows.ONE_ROW, ASSUMED),
    Answer(UNGROUPED_EMPTY_ROWS, OPENCYPHER, EmptyInputRows.ONE_ROW, OBSERVED, ("G4",)),
    Answer(GROUPED_EMPTY_ROWS, SQL, EmptyInputRows.NO_ROW, ASSUMED),
    Answer(GROUPED_EMPTY_ROWS, SPARQL, EmptyInputRows.NO_ROW, ASSUMED, ("K9",)),
    Answer(AVG_INTEGER, SQL, AvgResult.DECIMAL_25_6_TRUNCATED, VENDOR_DOC, ("R7", "R20"), (SQL_REF, TWELVE_HOURS)),
    Answer(AVG_INTEGER, SPARQL, AvgResult.DOUBLE, OBSERVED, ("K23",), (Finding("B13"), TWELVE_HOURS, Standard(SPARQL11, "17.3")), standard=AvgResult.XSD_DECIMAL),
    Answer(AVG_INTEGER_DECIMAL_CAST, SPARQL, AvgResult.DOUBLE, OBSERVED, ("K25",), (Finding("B13"),), standard=AvgResult.XSD_DECIMAL),
    Answer(MEDIAN_INTEGER, SQL, MedianResult.ROUNDED_HALF_AWAY_FROM_ZERO, OBSERVED, ("R9",), (Finding("B1"),)),
    Answer(MEDIAN_INTEGER, SPARQL, MedianResult.INTERPOLATED, OBSERVED, ("K16",)),
    Answer(PERCENTILE_CONT_INTEGER, SQL, MedianResult.INTERPOLATED, OBSERVED, ("R9",)),
    # --- arithmetic ---
    Answer(INTEGER_DIVISION, SPARQL, NumericKind.DOUBLE, OBSERVED, ("K23",), (Finding("B13"), Standard(SPARQL11, "17.3")), standard=NumericKind.DECIMAL),
    Answer(INTEGER_DIVISION, OPENCYPHER, NumericKind.DOUBLE, OBSERVED, ("G14",)),
    # --- match / path ---
    Answer(EDGE_BINDING, OPENCYPHER, EdgeBinding.EDGE_ISOMORPHISM, OBSERVED, ("G8",)),
    Answer(UNDIRECTED_SELF_LOOP_ROWS, OPENCYPHER, 1, OBSERVED, ("G7",)),
    Answer(VARIABLE_LENGTH_PATH_MODE, OPENCYPHER, PathMode.TRAIL, OBSERVED, ("G9",)),
    # --- query ---
    Answer(DEFAULT_DATASET, SPARQL, Dataset.ALL_GRAPHS, VENDOR_DOC, (), (KG_GUIDE,), standard=Dataset.DEFAULT_GRAPH),
    Answer(NAMES_CASE_SENSITIVE, OPENCYPHER, True, VENDOR_DOC, ("G16",), (PGE_REF,)),
    # --- accepts: openCypher ---
    Answer(accepts("match.second_clause"), OPENCYPHER, REFUSED, OBSERVED, ("G8",), (PGE_REF,), data_model="graph", error=4906, documented=ACCEPTED),
    Answer(accepts("pattern.chained_edges"), OPENCYPHER, REFUSED, OBSERVED, ("G8",), error=4905),
    Answer(accepts("pattern.incoming_arrow"), OPENCYPHER, REFUSED, OBSERVED, GRAPH_ROW),
    Answer(accepts("match.optional"), OPENCYPHER, REFUSED, OBSERVED, GRAPH_ROW),
    Answer(accepts("query.union"), OPENCYPHER, REFUSED, OBSERVED, GRAPH_ROW),
    Answer(accepts("query.with"), OPENCYPHER, REFUSED, OBSERVED, GRAPH_ROW),
    Answer(accepts("query.unwind"), OPENCYPHER, REFUSED, OBSERVED, GRAPH_ROW),
    Answer(accepts("expression.in_list"), OPENCYPHER, REFUSED, OBSERVED, GRAPH_ROW),
    Answer(accepts("expression.boolean_constant"), OPENCYPHER, REFUSED, OBSERVED, GRAPH_ROW),
    Answer(accepts("expression.is_not_null"), OPENCYPHER, REFUSED, OBSERVED, ("G3", "G14", "G15")),
    Answer(accepts("where.boolean_attribute"), OPENCYPHER, REFUSED, OBSERVED, ("G10",), error=4906),
    Answer(accepts("count.boolean_attribute"), OPENCYPHER, REFUSED, OBSERVED, ("G33",), (Finding("B14"),), error=4906),
    Answer(accepts("compare.boolean_attributes"), OPENCYPHER, REFUSED, OBSERVED, ("G33",), (Finding("B14"),), error=4906),
    Answer(accepts("group_key.boolean_attribute"), OPENCYPHER, ACCEPTED, OBSERVED, ("G33",)),
    Answer(accepts("compare.bigint_attribute_with_float"), OPENCYPHER, REFUSED, OBSERVED, ("G36",), error=4906),
    Answer(accepts("parameters.cast"), OPENCYPHER, REFUSED, OBSERVED, ("G2", "G2b"), error=7),
    Answer(accepts("contains.parameter"), OPENCYPHER, REFUSED, OBSERVED, ("G15",), error=4905),
    Answer(accepts("order_by.expression"), OPENCYPHER, REFUSED, OBSERVED, ("G5",)),
    Answer(accepts("skip.without_limit"), OPENCYPHER, REFUSED, OBSERVED, ("G5b",), error=4905),
    Answer(accepts("aggregate.count_distinct"), OPENCYPHER, REFUSED, OBSERVED, ("G4", "G24")),
    Answer(accepts("aggregate.avg"), OPENCYPHER, REFUSED, OBSERVED, ("G4", "G24")),
    # --- accepts: the collection query and SQL over collections ---
    Answer(accepts("filter.bare_parameter"), COLLECTION, REFUSED, VENDOR_DOC, ("D4", "D5", "D6"), (DOC_STORE,)),
    Answer(accepts("filter.in_list_parameter"), COLLECTION, REFUSED, OBSERVED, (), (VERIFICATION,)),
    Answer(accepts("like.parameter"), COLLECTION, REFUSED, OBSERVED, ("D18",), (Finding("B4"),)),
    Answer(accepts("like.cast_nvarchar_parameter"), COLLECTION, ACCEPTED, VENDOR_DOC, (), (DOC_STORE, Finding("B4"))),
    Answer(accepts("like.cast_nvarchar_length_parameter"), COLLECTION, REFUSED, OBSERVED, (), (Finding("B4"),)),
    Answer(accepts("query.boolean_constant"), COLLECTION, REFUSED, OBSERVED, ("D16",), (Finding("B3"),)),
    Answer(accepts("update.set_expression"), COLLECTION, REFUSED, VENDOR_DOC, ("D13",), (DOC_STORE, Finding("B5"))),
    Answer(accepts("update.set_unset_without_comma"), COLLECTION, REFUSED, VENDOR_DOC, (), (DOC_STORE, Finding("B5"))),
    Answer(accepts("union_all.collection_queries"), COLLECTION, REFUSED, OBSERVED, ("D23",), error=7),
    Answer(accepts("except.collection_queries"), COLLECTION, REFUSED, OBSERVED, ("D25",), error=7),
    Answer(accepts("subquery.nested_unnest_view"), SQL, REFUSED, OBSERVED, (), (Finding("B6"), VERIFICATION), data_model="doc"),
    # --- accepts: SPARQL ---
    Answer(accepts("select.ungrouped_variable"), SPARQL, REFUSED, ASSUMED, (), (Standard(SPARQL11, "11.4"), VERIFICATION), error=129),
    Answer(accepts("aggregate.median.without_group_by"), SPARQL, REFUSED, OBSERVED, ("K16",), (Finding("B2"),), error=129),
    Answer(accepts("path.range_quantifier"), SPARQL, REFUSED, VENDOR_DOC, ("K10",), (SPARQL_REF,)),
    # --- accepts: SQL ---
    Answer(accepts("offset.without_limit"), SQL, REFUSED, VENDOR_DOC, ("R6",), (SQL_REF,)),
    Answer(accepts("identifier.start"), SQL, REFUSED, OBSERVED, (), (Finding("B11"),)),
    # --- transactions ---
    Answer(JOINS_SQL_TRANSACTION, SPARQL, True, VENDOR_DOC, ("K15",), (SPARQL_REF,)),
    Answer(JOINS_SQL_TRANSACTION, COLLECTION, True, ASSUMED, ("D17",)),
    Answer(PEER_SEES_UNCOMMITTED, SQL, False, OBSERVED, ("R24",)),
    Answer(PEER_SEES_UNCOMMITTED, COLLECTION, False, OBSERVED, ("D26",)),
    Answer(PEER_SEES_UNCOMMITTED, SPARQL, False, OBSERVED, ("K27",)),
    Answer(PEER_SEES_UNCOMMITTED, OPENCYPHER, False, OBSERVED, ("G37",)),
    Answer(OWN_UNCOMMITTED_VISIBLE, OPENCYPHER, True, OBSERVED, ("G12",)),
    # --- equivalent forms ---
    Answer(HINT_PROFILE_EQUIVALENT, SQL, True, OBSERVED, HINT_FACTS, (HINTS,)),
    Answer(HINT_PROFILE_EQUIVALENT, COLLECTION, True, OBSERVED, ("D27",)),
    Answer(ANONYMOUS_BLOCK_EQUIVALENT, SQLSCRIPT, True, OBSERVED, ("P20",)),
    Answer(SET_PARTITION_EQUIVALENT, SQL, True, OBSERVED, ("R23",)),
    Answer(SET_PARTITION_EQUIVALENT, SQL, True, OBSERVED, ("D25",), data_model="doc"),
    Answer(SET_PARTITION_EQUIVALENT, SPARQL, True, OBSERVED, ("K26",)),
    Answer(NOT_AS_WRITTEN_EQUIVALENT, COLLECTION, True, OBSERVED, ("D19", "D20")),
    Answer(FROM_CLAUSE_EQUIVALENT, SPARQL, True, OBSERVED, ("K3",)),
)

DEFECTS = (
    Defect("A1", SPARQL, "result.double.serialization", Effect.LOST_PRECISION, Handling.WORKED_AROUND, "rdf", refs=(Finding("A1"), VERIFICATION), aliases=("H1",)),
    Defect("A2", SQL, "subquery.correlated_scalar.unnest_view", Effect.INTERNAL_ERROR, Handling.WITHHELD, "doc", error=3, refs=(Finding("A2"),), aliases=("H2",)),
    Defect("A3", SPARQL, "aggregate.sum_avg.unbound_value.predicate_with_two_datatypes", Effect.WRONG_ANSWER, Handling.WORKED_AROUND, "rdf", facts=("K20",), refs=(Finding("A3"), H3_CAMPAIGN), aliases=("H3",)),
    Defect("A4", SQL, "aggregate.sum.join.unnest_link_view.excluded_rows", Effect.OVERFLOW, Handling.WITHHELD, "doc", error=314, refs=(Finding("A4"), H3_CAMPAIGN), aliases=("H4",)),
    Defect("A5", SQL, "between.parameters.collection_field", Effect.REFUSAL, Handling.WORKED_AROUND, "doc", error=-10427, refs=(Finding("A5"),), aliases=("H5",)),
    Defect("A6", SPARQL, "filter.variable_bound_nowhere", Effect.REFUSAL, Handling.WORKED_AROUND, "rdf", facts=("K6",), refs=(Finding("A6"),), aliases=("H6",)),
    Defect("A7", OPENCYPHER, "match.vertex_label.homogeneous_workspace", Effect.REFUSAL, Handling.NOT_MET, "graph", error=260, facts=("G27",), refs=(Finding("A7"),)),
    Defect("A8", OPENCYPHER, "constant.double", Effect.LOST_PRECISION, Handling.WORKED_AROUND, "graph", facts=("G31",), refs=(Finding("A8"), TWELVE_HOURS)),
    Defect("A9", OPENCYPHER, "statement.two_table_calls.mixed_parameter_types", Effect.WRONG_ANSWER, Handling.WORKED_AROUND, "graph", facts=("G30", "G32", "G34"), refs=(Finding("A9"), TWELVE_HOURS)),
    Defect("A10", SPARQL, "filter.coalesce.optional_variable", Effect.WRONG_ANSWER, Handling.WORKED_AROUND, "rdf", facts=("K26",), refs=(Finding("A10"),)),
)

ERRORS = (
    ErrorClass(ErrorKind.CONNECTION_LOST, frozenset({-10709, -10807, -10108, -10821, -10806})),
    ErrorClass(ErrorKind.TIMEOUT, frozenset({613})),
    ErrorClass(ErrorKind.TIMEOUT, marks=("timeout", "abort"), all_marks=True, ignore_case=True),
    ErrorClass(ErrorKind.REJECTION, frozenset({129}), ("generic. Error -", "Unsupported functionality", "supports only"), refs=(VERIFICATION,)),
    ErrorClass(ErrorKind.INTERNAL, frozenset({2, 3, 129, 2048})),
    ErrorClass(ErrorKind.INTERNAL, marks=("internal error", "assertion", "must not reach here"), ignore_case=True, facts=("G35",)),
    ErrorClass(ErrorKind.REJECTION, facts=("R17", "G22")),
)

CATALOG = Catalog("hana", Language, ANSWERS, DEFECTS, ERRORS)
