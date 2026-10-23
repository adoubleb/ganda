"""The rule records (`ganda.core.rules`): the schema `validate` enforces, the ordered
error classification and the divergences a catalog derives."""
import enum

from ganda.core.rules.model import IR, Answer, Basis, Catalog, Defect, Effect, ErrorClass, ErrorKind, Finding, Handling, Question, VendorDoc, Verdict, validate
from ganda.core.rules.render import render
from ganda.core.rules.vocabulary import NULLS_ASC, NullPlacement


class Lang(enum.StrEnum):
    A = "a"
    B = "b"


DOC = VendorDoc("Manual", "1.0")
FLAG = Question("value.flag", bool)


def _catalog(*answers, defects=(), errors=()):
    return Catalog("toy", Lang, tuple(answers), tuple(defects), tuple(errors))


def test_a_well_formed_catalog_validates():
    c = _catalog(
        Answer(NULLS_ASC, Lang.A, NullPlacement.FIRST, Basis.VENDOR_DOC, refs=(DOC,)),
        Answer(NULLS_ASC, Lang.B, NullPlacement.LAST, Basis.OBSERVED, ("X1",)),
        defects=(Defect("D1", Lang.A, "query.crash", Effect.INTERNAL_ERROR, Handling.WITHHELD, refs=(Finding("D1"),)),),
    )
    assert validate(c) == []
    ((q, answers),) = c.divergences()
    assert q == NULLS_ASC and [a.language for a in answers] == [Lang.A, Lang.B]


def test_validate_names_every_broken_field():
    bad = validate(_catalog(
        Answer(Question("Not An Identifier", bool), Lang.A, True, Basis.ASSUMED),
        Answer(FLAG, Lang.A, "yes", Basis.ASSUMED),
        Answer(FLAG, Lang.A, True, Basis.ASSUMED),
        Answer(NULLS_ASC, Lang.A, NullPlacement.LAST, Basis.OBSERVED),
        Answer(NULLS_ASC, Lang.B, NullPlacement.LAST, Basis.VENDOR_DOC, documented=NullPlacement.LAST),
        Answer(Question("accepts.thing", Verdict), Lang.A, Verdict.ACCEPTED, Basis.ASSUMED, error=7),
        Answer(Question("value.ir_only", bool), IR, True, Basis.ASSUMED),
        defects=(Defect("D1", Lang.A, "Crash!", Effect.REFUSAL, Handling.NOT_MET), Defect("D1", Lang.B, "query.x", Effect.REFUSAL, Handling.NOT_MET, refs=(Finding("D1"),))),
    ))
    text = "\n".join(bad)
    for expected in ("no dotted identifier", "outside the domain", "answered twice", "names the probe facts", "cites the vendor's document", "another value of the domain", "only a refusal carries an error code", "only they, are definitions", "the trigger is no dotted identifier", "a defect cites its finding", "used twice"):
        assert expected in text, expected


def test_error_classes_match_in_order():
    c = _catalog(errors=(
        ErrorClass(ErrorKind.TIMEOUT, frozenset({1})),
        ErrorClass(ErrorKind.REJECTION, frozenset({2}), ("ordinary",)),
        ErrorClass(ErrorKind.INTERNAL, frozenset({2})),
        ErrorClass(ErrorKind.INTERNAL, marks=("Crash", "Fatal"), all_marks=True, ignore_case=True),
        ErrorClass(ErrorKind.REJECTION),
    ))
    assert c.classify(1, "") is ErrorKind.TIMEOUT
    assert c.classify(2, "an ordinary refusal") is ErrorKind.REJECTION
    assert c.classify(2, "boom") is ErrorKind.INTERNAL
    assert c.classify(9, "crash: fatal") is ErrorKind.INTERNAL
    assert c.classify(9, "crash only") is ErrorKind.REJECTION


def test_render_is_tables_of_fields():
    c = _catalog(Answer(Question("accepts.thing", Verdict), Lang.A, Verdict.REFUSED, Basis.OBSERVED, ("X1",), error=7))
    text = render(c)
    assert "| accepts.thing | a | refused [7] | observed | X1 |" in text
    assert render(c) == text
