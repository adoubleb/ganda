"""ArangoDB's rule catalog (`ganda/arangodb/rules/`) against the code it describes: the
probe facts that check it, the defects they reproduce, the error classes the connection
layer classifies through, and the generated RULES.md."""
import ast
import pathlib

from ganda.core.rules.model import ErrorKind, validate
from ganda.arangodb.rules import CATALOG, read_index, rendered

ARANGO = pathlib.Path(__file__).resolve().parents[2] / "ganda" / "arangodb"


def _probe_ids() -> list:
    tree = ast.parse((ARANGO / "scripts" / "probe.py").read_text())
    return [n.args[0].value for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "fact"]


def test_every_probe_fact_has_its_own_id():
    ids = _probe_ids()
    assert len(ids) == len(set(ids)) and all(i[0] in "VSWLTA" and i[1:].isdigit() for i in ids)


def test_the_catalog_is_well_formed_and_its_checks_name_probe_facts():
    assert validate(CATALOG, set(_probe_ids())) == []


def test_every_rule_is_checked_by_a_fact_and_read_by_code():
    assert CATALOG.unchecked() == []
    index = read_index()
    unread = [k for k, reads in index["rules"].items() if not reads and k[1].value != "ir"]
    assert unread == []


def test_the_defects_are_a1_a2_a25_a40_a41_each_reproduced_and_read():
    assert [d.id for d in CATALOG.defects] == ["A1", "A2", "A25", "A40", "A41"]
    assert CATALOG.unreproduced() == []
    index = read_index()
    assert all(index["defects"][d.id] for d in CATALOG.defects)


def test_the_error_classes_read_an_errornum_as_the_connection_layer_does():
    assert CATALOG.classify(1500, "query killed") is ErrorKind.TIMEOUT
    assert CATALOG.classify(1600, "cursor not found") is ErrorKind.TIMEOUT
    assert CATALOG.classify(0, "the request timed out") is ErrorKind.TIMEOUT
    assert CATALOG.classify(1577, "could not use index hint") is ErrorKind.REJECTION
    # anything else is the HTTP status's business (a 5xx answered is internal)
    assert CATALOG.classify(1210, "unique constraint violated") is None
    assert CATALOG.classify(1542, "invalid argument type") is None


def test_the_probe_names_what_a_failing_fact_checks_and_who_reads_it(monkeypatch):
    from ganda.arangodb.scripts import probe

    assert probe.checked_by("S8") == ["accepts.bind_variable.undeclared (aql)"]
    assert probe.checked_by("L1") == ["no rule"]
    assert probe.checked_by("A40") == ["A40 (aql)"]
    index = read_index()
    (a41,) = probe.consequences("A41", index)
    assert a41.startswith("A41 (traversal) no longer reproduces: read by concrete_syntax/predicate.py:")
    assert probe.consequences("T1", index) == ["checks no rule"]
    monkeypatch.setattr(probe, "failures", ["A1", "A2", "L2"])
    assert probe._sort_failures(CATALOG) == (["L2"], {"A1": ["A1"], "A2": ["A2"]})
    monkeypatch.setattr(probe, "failures", ["V2", "A40"])  # V2: two rules; A40: a defect
    assert probe._sort_failures(CATALOG) == (["V2"], {"A40": ["A40"]})


def test_the_generated_rules_are_current():
    assert (ARANGO / "rules" / "RULES.md").read_text() == rendered(), "run `python3 -m ganda arangodb rules --write`"
