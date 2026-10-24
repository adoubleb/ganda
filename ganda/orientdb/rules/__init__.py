"""OrientDB's rule catalog, the lookups provider code reads it through
(`ganda.core.rules.lookup`) and the baseline: the build the probe last verified
(`ganda.core.rules.baseline`)."""
import pathlib

from ganda.core.rules.baseline import Baseline
from ganda.core.rules.lookup import Lookup
from ganda.core.rules.model import IR, Catalog  # noqa: F401 -- IR for the readers
from ganda.orientdb.rules.catalog import DEFECTS, ERRORS, RULES, Subject
from ganda.orientdb.rules.checks import CHECKS

CATALOG = Catalog("orientdb", Subject, RULES, DEFECTS, ERRORS, CHECKS)
BASELINE = Baseline(pathlib.Path(__file__).with_name("baseline.json"))
SOURCES = ["ganda/orientdb"]
EXCLUDE = ("rules", "scripts")

_lookup = Lookup(CATALOG)
answer, refused, alike, assumes, works_around = _lookup.answer, _lookup.refused, _lookup.alike, _lookup.assumes, _lookup.works_around


def read_index() -> dict:
    """Who reads each rule and defect (`lookup.read_by` over `SOURCES`)."""
    from ganda.core.rules.lookup import read_by, reads

    root = pathlib.Path(__file__).resolve().parents[3]
    return read_by(CATALOG, reads(CATALOG, root, SOURCES, EXCLUDE))


def rendered() -> str:
    from ganda.core.rules.render import render

    return render(CATALOG, read_index(), strip="ganda/orientdb/", baseline=BASELINE.read())
