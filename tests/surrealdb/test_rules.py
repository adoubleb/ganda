"""SurrealDB's rule catalog (`ganda/surrealdb/rules/`) against the code it describes: the
probe checks that check it, the defects they reproduce and the generated RULES.md."""
import ast
import pathlib

from ganda.core.rules.model import validate
from ganda.surrealdb.rules import CATALOG, read_index, rendered

SURREAL = pathlib.Path(__file__).resolve().parents[2] / "ganda" / "surrealdb"


def _probe_ids() -> list:
    tree = ast.parse((SURREAL / "rules" / "probe.py").read_text())
    return [n.args[0].value for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "check"]


def test_every_probe_check_has_its_own_id_and_checks_something():
    ids = _probe_ids()
    assert len(ids) == len(set(ids)) and all(i.startswith("C") and i[1:].isdigit() for i in ids)
    assert set(ids) == set(CATALOG.checks), "every probe check checks a rule, a defect or an error class"


def test_the_catalog_is_well_formed_and_its_checks_name_probe_checks():
    assert validate(CATALOG, set(_probe_ids())) == []


def test_every_rule_is_checked_and_read_and_every_defect_reproduced_and_avoided():
    assert CATALOG.unchecked() == []
    assert CATALOG.unreproduced() == []
    index = read_index()
    unread = [r for r in CATALOG.rules if not index["rules"].get((r.question.name, r.subject))]
    assert unread == [], [f"{r.question.name} ({r.subject})" for r in unread]
    assert [d.id for d in CATALOG.defects] == ["F28", "F29", "F6", "F21"]
    assert all(index["defects"].get(d.id) for d in CATALOG.defects)


def test_the_probe_names_what_a_failing_check_checks_and_who_reads_it():
    from ganda.surrealdb.rules import probe

    assert probe.checked_by("C40") == ["F28 (surql@index)"]
    assert probe.checked_by("C2") == ["compare.none_and_null.below_every_value (surql)"]
    assert probe.checked_by("C999") == ["no rule"]
    (f28,) = probe.consequences("C40", read_index())
    assert f28.startswith("F28 (surql@index) no longer reproduces: read by known_bugs/predicate.py:")


def test_a_fixed_defect_does_not_block_the_baseline_and_a_changed_rule_does(monkeypatch):
    from ganda.surrealdb.rules import probe

    monkeypatch.setattr(probe, "failures", ["C40", "C41", "C44", "C45"])
    assert probe._sort_failures(CATALOG) == ([], {"F28": ["C40", "C45"], "F6": ["C41"], "F29": ["C44"]})
    monkeypatch.setattr(probe, "failures", ["C40", "C2", "C999"])
    assert probe._sort_failures(CATALOG) == (["C2", "C999"], {"F28": ["C40"]})


def test_the_error_classes_classify_the_engine_messages():
    from ganda.core.rules.model import ErrorKind

    assert CATALOG.classify(0, "The query was not executed because it exceeded the timeout: 1ns") is ErrorKind.TIMEOUT
    assert CATALOG.classify(0, "Internal error: something") is ErrorKind.INTERNAL
    assert CATALOG.classify(0, "Database index `ix` already contains 1, with record `t:a`") is None


def test_the_generated_rules_are_current():
    assert (SURREAL / "rules" / "RULES.md").read_text() == rendered(), "run `python3 -m ganda surrealdb rules --write`"


def test_surrealdb_compares_data_models_only():
    from ganda.core.data.generator import build
    from ganda.core.oracle.case import comparable_pairs, data_model_of
    from ganda.core.query.generators.registry import all_cases
    from ganda.surrealdb import PROVIDER

    cfg = PROVIDER.config_class()
    schema, inst, rng = build(3, cfg, PROVIDER)
    seen = 0
    for case in all_cases(rng, schema, inst, cfg=cfg):
        for a, b in comparable_pairs(case, case.variants):
            assert data_model_of(a) != data_model_of(b) and case.spellings.get(a, "") == case.spellings.get(b, "")
            seen += 1
    assert seen
