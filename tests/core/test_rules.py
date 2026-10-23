"""The rule records (`ganda.core.rules`): the schema `validate` enforces, the divergences
and unchecked rules a catalog derives, and the ordered error classification."""
import enum

from ganda.core.rules.model import IR, Catalog, Defect, ErrorClass, ErrorKind, Kind, Question, Rule, Verdict, validate
from ganda.core.rules.render import render
from ganda.core.rules.vocabulary import NULLS_ASC, NullPlacement


class Subj(enum.StrEnum):
    A = "a"
    B = "b"


ASK_THING = Question("accepts.thing", Verdict, Kind.ASK)


def _catalog(*rules, defects=(), errors=(), checks=None):
    return Catalog("toy", Subj, tuple(rules), tuple(defects), tuple(errors), checks or {})


def test_a_well_formed_catalog_validates_and_derives():
    first, last = Rule(NULLS_ASC, Subj.A, NullPlacement.FIRST), Rule(NULLS_ASC, Subj.B, NullPlacement.LAST)
    c = _catalog(first, last, Rule(ASK_THING, Subj.A, Verdict.REFUSED), defects=(Defect("D1", Subj.A),), checks={"X1": (first,)})
    assert validate(c, {"X1"}) == []
    ((q, rules),) = c.divergences()
    assert q == NULLS_ASC and [r.subject for r in rules] == [Subj.A, Subj.B]
    assert c.checked_by(first) == ["X1"] and c.unchecked() == [last, c.rules[2]]


def test_validate_names_every_broken_field():
    flag = Question("value.flag", bool, Kind.COMPARE)
    bad = validate(_catalog(
        Rule(Question("Not An Identifier", bool, Kind.COMPARE), Subj.A, True),
        Rule(flag, Subj.A, "yes"),
        Rule(flag, Subj.A, True),
        Rule(Question("value.flag", int, Kind.COMPARE), Subj.B, 1),
        Rule(Question("accepts.other", bool, Kind.ASK), Subj.A, True),
        Rule(Question("equivalent.form", Verdict, Kind.EQUIVALENT), Subj.A, Verdict.ACCEPTED),
        Rule(ASK_THING, IR, Verdict.ACCEPTED),
        defects=(Defect("D1", Subj.A), Defect("D1", Subj.B)),
        checks={"X9": (Rule(flag, Subj.B, False),)},
    ), {"X1"})
    text = "\n".join(bad)
    for expected in ("no dotted identifier", "outside the domain", "answered twice", "two questions share the name", "ask question", "equivalent question", "IR answers compare questions only", "no such probe fact", "no rule of the catalog", "used twice"):
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


def test_render_is_tables_of_the_three_fields_and_the_checks():
    r = Rule(ASK_THING, Subj.A, Verdict.REFUSED)
    text = render(_catalog(r, Rule(ASK_THING, Subj.B, Verdict.ACCEPTED), checks={"X1": (r,)}))
    assert "| accepts.thing | a | refused | X1 |" in text and "| accepts.thing | b | accepted | unchecked |" in text
