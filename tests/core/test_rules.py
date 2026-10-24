"""The rule records (`ganda.core.rules`): the schema `validate` enforces, the divergences
and unchecked rules a catalog derives, the ordered error classification and the baseline."""
import enum

from ganda.core.rules.baseline import Baseline
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


def test_a_check_reproduces_a_defect_and_validate_knows_the_defects():
    d1, stray = Defect("D1", Subj.A), Defect("D9", Subj.B)
    c = _catalog(Rule(ASK_THING, Subj.A, Verdict.REFUSED), defects=(d1, Defect("D2", Subj.B)), checks={"X1": (d1,)})
    assert validate(c, {"X1"}) == [] and c.checked_by(d1) == ["X1"] and [d.id for d in c.unreproduced()] == ["D2"]
    assert "| D1 | a | X1 |" in render(c) and "| D2 | b | unchecked |" in render(c)
    assert "D9 / b is no defect of the catalog" in "\n".join(validate(_catalog(defects=(d1,), checks={"X1": (stray,)})))


def test_the_baseline_blocks_on_a_rule_and_records_a_fixed_defect(tmp_path):
    base = Baseline(tmp_path / "baseline.json")
    assert base.warning("toy", "1.0") is None  # no baseline yet: nothing to compare
    assert base.record("1.0", ["X1"], {}, ["X2"]).startswith("baseline not written") and base.read() == {}
    assert base.record("1.0", ["X10", "X2", "X1"], {"D1": ["X3"]}, []).startswith("baseline written")
    assert base.read()["version"] == "1.0" and base.read()["held"] == ["X1", "X2", "X10"] and base.read()["fixed"] == {"D1": ["X3"]}
    assert base.record("1.1", ["X3"], {}, [], partial=True).startswith("baseline not written")
    base.record("1.0", ["X3"], {"D2": ["X4"]}, [], partial=True)  # D1 reproduces again, D2 fixed
    assert base.read()["held"] == ["X1", "X2", "X3", "X10"] and base.read()["fixed"] == {"D2": ["X4"]}
    assert base.warning("toy", "1.0") is None and base.warning("toy", None) is None
    assert "the server is 1.1, the baseline 1.0" in base.warning("toy", "1.1")


def test_render_shows_the_baseline_and_each_defect_on_it():
    d1, d2, d3 = Defect("D1", Subj.A), Defect("D2", Subj.A), Defect("D3", Subj.B)
    c = _catalog(Rule(ASK_THING, Subj.A, Verdict.REFUSED), defects=(d1, d2, d3), checks={"X1": (d1,), "X2": (d2,), "X3": (d2,), "X4": (d3,), "X5": ()})
    text = render(c, baseline={"version": "1.0", "probed": "2026-10-24", "held": ["X1", "X3"], "fixed": {"D2": ["X2"], "D3": ["X4"]}})
    assert "baseline: 1.0, every check of a rule held; not run there: X5" in text
    assert "| D1 | a | X1 | reproduces |" in text and "| D2 | a | X2, X3 | fixed: X2 |" in text and "| D3 | b | X4 | fixed |" in text
    assert "baseline: none" in render(c, baseline={})


def test_mutation_alternatives_cover_each_domain():
    from ganda.core.rules.mutation import alternatives, mutants

    first = Rule(NULLS_ASC, Subj.A, NullPlacement.FIRST)
    assert alternatives(first) == [m for m in NullPlacement if m is not NullPlacement.FIRST]
    assert alternatives(Rule(Question("value.flag", bool, Kind.COMPARE), Subj.A, True)) == [False]
    assert alternatives(Rule(Question("match.rows", int, Kind.COMPARE), Subj.A, 2)) == [3]
    assert alternatives(Rule(Question("exact.below", int, Kind.EXACT), Subj.A, 2**62)) == [2**31]
    assert alternatives(Rule(Question("filter.ops", str, Kind.COMPARE), Subj.A, "= <>")) == ["="]
    assert alternatives(Rule(Question("filter.wildcard", str, Kind.COMPARE), Subj.A, "?")) == ["@"]
    labels = [label for _, label in mutants(_catalog(first, defects=(Defect("D1", Subj.A),)))]
    assert labels == [f"order.nulls.asc (a): first -> {m.value}" for m in NullPlacement if m is not NullPlacement.FIRST] + ["without D1 (a)"]
