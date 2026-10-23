"""SAP HANA's rule catalog (`ganda/hana/rules/`) against the code it describes: the probe
facts that check it, the findings its defects name, the connection's error
classification and the generated RULES.md."""
import pathlib
import types

import pytest

from ganda.core.engine.provider import EngineCrash, EngineError
from ganda.core.rules.model import ErrorKind, validate
from ganda.core.rules.render import render
from ganda.hana.engine import connection
from ganda.hana.rules import CATALOG
from ganda.hana.scripts import probe

HANA = pathlib.Path(__file__).resolve().parents[2] / "ganda" / "hana"


def test_the_catalog_is_well_formed_and_its_checks_name_probe_facts():
    assert validate(CATALOG, {f.id for f in probe.FACTS}) == []


def test_every_defect_has_its_finding():
    names = [p.name for p in (HANA / "findings").iterdir() if p.is_dir()]
    assert [d.id for d in CATALOG.defects if not any(n.startswith(f"{d.id}-") for n in names)] == []


def test_the_connection_holds_the_catalogs_error_codes():
    by_kind = {}
    for e in CATALOG.errors:
        by_kind.setdefault(e.kind, []).append(e)
    assert set(connection.CONNECTION_LOST) == set(by_kind[ErrorKind.CONNECTION_LOST][0].codes)
    assert set(connection.TIMEOUT_CODES) == set(by_kind[ErrorKind.TIMEOUT][0].codes)
    assert set(connection.INTERNAL_CODES) == set(by_kind[ErrorKind.INTERNAL][0].codes)
    assert tuple(connection.INTERNAL_TEXTS) == by_kind[ErrorKind.INTERNAL][1].marks
    assert tuple(connection.REJECTION_129) == by_kind[ErrorKind.REJECTION][0].marks


SAMPLES = [(-10709, "Socket closed by peer"), (613, "execution aborted by timeout"), (1, "Timeout: statement aborted"), (129, "generic. Error - syntax"), (129, "transaction rolled back by an internal error"),
           (3, "fatal error: Execution flow must not reach here"), (2048, "column store error"), (4906, "GraphEngine: OpenCypher compilation error"), (7, "an assertion failed"), (301, "unique constraint violated"),
           (129, "Unsupported functionality: MEDIAN"), (4901, "compilation failed: exception 42"), (0, "")]


@pytest.mark.parametrize("code,text", SAMPLES)
def test_the_catalog_classifies_errors_as_the_connection_does(code, text):
    e = connection._error(types.SimpleNamespace(errorcode=code, errortext=text))
    seen = ErrorKind.CONNECTION_LOST if isinstance(e, EngineCrash) else ErrorKind.TIMEOUT if e.timeout else ErrorKind.INTERNAL if e.internal else ErrorKind.REJECTION
    assert isinstance(e, (EngineCrash, EngineError))
    assert CATALOG.classify(code, text) is seen


def test_the_probe_names_the_rules_a_failing_fact_checks():
    assert probe.backed_by("G5") == ["order.nulls.asc (opencypher)"]
    assert probe.backed_by("K0") == ["no rule"]


def test_the_generated_rules_are_current():
    assert (HANA / "rules" / "RULES.md").read_text() == render(CATALOG), "run `python3 -m ganda hana rules --write`"
