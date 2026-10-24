"""OrientDB's rule catalog (`ganda/orientdb/rules/`) against the code it describes: the probe
checks that check it, the defects they reproduce and the generated RULES.md."""
import ast
import pathlib

from ganda.core.rules.model import validate
from ganda.orientdb.rules import CATALOG, rendered

ORIENT = pathlib.Path(__file__).resolve().parents[2] / "ganda" / "orientdb"


def _probe_ids() -> list:
    tree = ast.parse((ORIENT / "scripts" / "probe_osql.py").read_text())
    return [n.args[0].value for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "check"]


def test_every_probe_check_has_its_own_id():
    ids = _probe_ids()
    assert len(ids) == len(set(ids)) and all(i == f"C{n}" for n, i in enumerate(sorted(ids, key=lambda i: int(i[1:])), 1))


def test_the_catalog_is_well_formed_and_its_checks_name_probe_checks():
    assert validate(CATALOG, set(_probe_ids())) == []


def test_the_defects_are_o1_to_o46_but_the_retired_each_reproduced_by_a_probe_check():
    retired = {"O3", "O4"}  # O3: the `index:` door is no longer asked; O4: fixed in 3.2.57
    assert [d.id for d in CATALOG.defects] == [f"O{n}" for n in range(1, 47) if f"O{n}" not in retired]
    assert CATALOG.unreproduced() == []


def test_the_probe_names_what_a_failing_check_checks_and_who_reads_it():
    from ganda.orientdb.rules import read_index
    from ganda.orientdb.scripts import probe_osql

    assert probe_osql.checked_by("C35") == ["accepts.projection.comparison (osql)"]
    assert probe_osql.checked_by("C148") == ["exact.sum.int32.below (osql)", "O19 (osql)"]
    assert probe_osql.checked_by("C1") == ["no rule"]
    index = read_index()
    (o18,) = probe_osql.consequences("C122", index)
    assert o18.startswith("O18 (osql@index) no longer reproduces: read by known_bugs/guards.py:")
    assert probe_osql.consequences("C1", index) == ["checks no rule"]


def test_a_fixed_defect_does_not_block_the_baseline_and_a_changed_rule_does(monkeypatch):
    from ganda.orientdb.scripts import probe_osql

    monkeypatch.setattr(probe_osql, "failures", ["C122", "C205", "C202"])
    assert probe_osql._sort_failures(CATALOG) == ([], {"O18": ["C122", "C205"], "O19": ["C202"]})
    monkeypatch.setattr(probe_osql, "failures", ["C122", "C148", "C75"])  # C148: a rule and O19; C75: nothing
    assert probe_osql._sort_failures(CATALOG) == (["C148", "C75"], {"O18": ["C122"]})


def test_the_generated_rules_are_current():
    assert (ORIENT / "rules" / "RULES.md").read_text() == rendered(), "run `python3 -m ganda orientdb rules --write`"


def test_the_code_depends_on_what_it_reads():
    """A sample of the catalog mutation check (`ganda orientdb rules --mutate` runs every
    mutant over 20 seeds): an `assumes` and a `works_around` fail at import, a bound
    changes the generated cases."""
    from ganda.core.rules import mutation

    only = ["projection.keeps_absent_field", "exact.sum.int32", "without O18"]
    got = {o.label.split(":")[0]: o for o in mutation.check("orientdb", CATALOG, seeds=4, only=only)}
    assert got["projection.keeps_absent_field (osql)"].caught == "import" and "concrete_syntax/traversal.py" in got["projection.keeps_absent_field (osql)"].detail
    assert got["without O18 (osql@index)"].caught == "import" and "known_bugs/guards.py" in got["without O18 (osql@index)"].detail
    assert got["exact.sum.int32.below (osql)"].caught == "output"
