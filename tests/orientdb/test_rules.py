"""OrientDB's rule catalog (`ganda/orientdb/rules/`) against the code it describes: the probe
checks that check it, the defects and the generated RULES.md."""
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


def test_the_defects_are_o1_to_o46():
    assert [d.id for d in CATALOG.defects] == [f"O{n}" for n in range(1, 47)]


def test_the_probe_names_the_rules_a_failing_check_checks():
    from ganda.orientdb.scripts import probe_osql

    assert probe_osql.checked_by("C12") == ["order.nulls.asc (osql)"]
    assert probe_osql.checked_by("C1") == ["no rule"]


def test_the_generated_rules_are_current():
    assert (ORIENT / "rules" / "RULES.md").read_text() == rendered(), "run `python3 -m ganda orientdb rules --write`"
