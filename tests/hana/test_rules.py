"""SAP HANA's rule catalog (`ganda/hana/rules/`) against the code it describes: the probe
facts that check it, the findings its defects name, the connection's error
classification and the generated RULES.md."""
import pathlib
import types

import pytest

from ganda.core.engine.provider import EngineCrash, EngineError
from ganda.core.rules.model import ErrorKind, validate
from ganda.hana.engine import connection
from ganda.hana.rules import CATALOG, read_index, rendered
from ganda.hana.scripts import probe

HANA = pathlib.Path(__file__).resolve().parents[2] / "ganda" / "hana"


def test_the_catalog_is_well_formed_and_its_checks_name_probe_facts():
    assert validate(CATALOG, {f.id for f in probe.FACTS}) == []


def test_every_defect_has_its_finding():
    names = [p.name for p in (HANA / "findings").iterdir() if p.is_dir()]
    assert [d.id for d in CATALOG.defects if not any(n.startswith(f"{d.id}-") for n in names)] == []


# (code, message, the kind the engine's error is): connection lost, timeouts by code and by
# message, the triple store's 129 wrapping an ordinary refusal, internal failures by code
# and by message, and ordinary refusals (a cypher compile error naming an exception too)
SAMPLES = [
    (-10709, "Socket closed by peer", ErrorKind.CONNECTION_LOST),
    (613, "execution aborted by timeout", ErrorKind.TIMEOUT),
    (1, "Timeout: statement aborted", ErrorKind.TIMEOUT),
    (129, "generic. Error - syntax", ErrorKind.REJECTION),
    (129, "Unsupported functionality: MEDIAN", ErrorKind.REJECTION),
    (129, "transaction rolled back by an internal error", ErrorKind.INTERNAL),
    (3, "fatal error: Execution flow must not reach here", ErrorKind.INTERNAL),
    (2048, "column store error", ErrorKind.INTERNAL),
    (7, "an assertion failed", ErrorKind.INTERNAL),
    (4906, "GraphEngine: OpenCypher compilation error", ErrorKind.REJECTION),
    (4901, "compilation failed: exception 42", ErrorKind.REJECTION),
    (301, "unique constraint violated", ErrorKind.REJECTION),
    (0, "", ErrorKind.REJECTION),
]


@pytest.mark.parametrize("code,text,kind", SAMPLES)
def test_the_connection_classifies_errors_by_the_catalog(code, text, kind):
    e = connection._error(types.SimpleNamespace(errorcode=code, errortext=text))
    seen = ErrorKind.CONNECTION_LOST if isinstance(e, EngineCrash) else ErrorKind.TIMEOUT if e.timeout else ErrorKind.INTERNAL if e.internal else ErrorKind.REJECTION
    assert isinstance(e, (EngineCrash, EngineError)) and seen is kind and CATALOG.classify(code, text) is kind


def test_the_probe_names_the_rules_a_failing_fact_checks():
    assert probe.backed_by("G5") == ["order.nulls.asc (opencypher)"]
    assert probe.backed_by("K0") == ["no rule"]


def test_the_generated_rules_are_current():
    assert (HANA / "rules" / "RULES.md").read_text() == rendered(), "run `python3 -m ganda hana rules --write`"
