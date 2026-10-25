"""OrientDB's rule catalog (`ganda/orientdb/rules/`) against the code it describes: the probe
checks that check it, the defects they reproduce and the generated RULES.md."""
import ast
import pathlib

from ganda.core.rules.model import validate
from ganda.orientdb.rules import CATALOG, rendered

ORIENT = pathlib.Path(__file__).resolve().parents[2] / "ganda" / "orientdb"


def _probe_ids() -> list:
    tree = ast.parse((ORIENT / "scripts" / "probe.py").read_text())
    return [n.args[0].value for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "check"]


def test_every_probe_check_has_its_own_id_and_checks_something():
    # ids are never reused: a deleted check leaves a gap
    ids = _probe_ids()
    assert len(ids) == len(set(ids)) and all(i.startswith("C") and i[1:].isdigit() for i in ids)
    assert set(ids) == set(CATALOG.checks), "every probe check checks a rule, a defect or an error class"


def test_the_catalog_is_well_formed_and_its_checks_name_probe_checks():
    assert validate(CATALOG, set(_probe_ids())) == []


def test_the_defects_are_o1_to_o46_but_the_retired_each_reproduced_by_a_probe_check():
    # O3: the `index:` door is no longer asked; O4: fixed in 3.2.57; O30: the null rows
    # under arithmetic matter only against the plain filter, which is no longer compared
    # O11, O28, O33: they answer alike on every data model (two guards-off campaigns:
    # 0 failing of 85-258 compared pairs each)
    # O32: its failing pairs were other defects underneath
    retired = {"O3", "O4", "O11", "O28", "O29", "O30", "O32", "O33", "O34", "O42"}
    assert [d.id for d in CATALOG.defects] == [f"O{n}" for n in range(1, 47) if f"O{n}" not in retired]
    assert CATALOG.unreproduced() == []


def test_the_probe_names_what_a_failing_check_checks_and_who_reads_it():
    from ganda.orientdb.rules import read_index
    from ganda.orientdb.scripts import probe

    assert probe.checked_by("C333") == ["expand.embedded.wrapped_under_value (osql)"]
    assert probe.checked_by("C148") == ["exact.sum.int32.below (osql)", "O19 (osql)"]
    assert probe.checked_by("C5") == ["no rule"]  # C5 was deleted
    index = read_index()
    (o18,) = probe.consequences("C122", index)
    assert o18.startswith("O18 (osql@index) no longer reproduces: read by known_bugs/select.py:")
    assert probe.consequences("C5", index) == ["checks no rule"]


def test_a_fixed_defect_does_not_block_the_baseline_and_a_changed_rule_does(monkeypatch):
    from ganda.orientdb.scripts import probe

    monkeypatch.setattr(probe, "failures", ["C122", "C205", "C202"])
    assert probe._sort_failures(CATALOG) == ([], {"O18": ["C122", "C205"], "O19": ["C202"]})
    monkeypatch.setattr(probe, "failures", ["C122", "C148", "C75"])  # C148: a rule and O19; C75: nothing
    assert probe._sort_failures(CATALOG) == (["C148", "C75"], {"O18": ["C122"]})


def test_the_generated_rules_are_current():
    assert (ORIENT / "rules" / "RULES.md").read_text() == rendered(), "run `python3 -m ganda orientdb rules --write`"


def test_the_code_depends_on_what_it_reads():
    """A sample of the catalog mutation check (`ganda orientdb rules --mutate` runs every
    mutant over 20 seeds): an `assumes` and a `works_around` fail at import, a bound
    changes the generated cases."""
    from ganda.core.rules import mutation

    only = ["expand.embedded", "aggregate.sum.integer", "without O18"]
    got = {o.label.split(":")[0]: o for o in mutation.check("orientdb", CATALOG, seeds=4, only=only)}
    assert got["expand.embedded.wrapped_under_value (osql)"].caught == "import" and "concrete_syntax/query/traversal.py" in got["expand.embedded.wrapped_under_value (osql)"].detail
    assert got["without O18 (osql@index)"].caught == "import" and "known_bugs/select.py" in got["without O18 (osql@index)"].detail
    assert got["aggregate.sum.integer.exact_below (osql)"].caught == "output"


def test_orientdb_compares_data_models_only():
    from ganda.core.data.generator import build
    from ganda.core.oracle.case import comparable_pairs, data_model_of
    from ganda.core.query.generators.registry import all_cases
    from ganda.orientdb import PROVIDER

    cfg = PROVIDER.config_class()
    schema, inst, rng = build(3, cfg, PROVIDER)
    seen = 0
    for case in all_cases(rng, schema, inst, cfg=cfg):
        for a, b in comparable_pairs(case, case.variants):
            assert data_model_of(a) != data_model_of(b) and case.spellings.get(a, "") == case.spellings.get(b, "")
            seen += 1
    assert seen
