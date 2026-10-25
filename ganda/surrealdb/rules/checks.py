"""Which rules (and error classes) each probe check (`rules/probe.py`, ids `C<n>`,
never reused) checks, and which defects it reproduces -- every check is listed: a check
is listed against a rule only where it asserts that rule's answer, against a defect
only where its expected answer is the defect's."""
from __future__ import annotations

from ganda.surrealdb.rules.catalog import DEFECTS

_RULE_CHECKS: dict = {}

REPRODUCED_BY: dict = {}

_DEFECT = {d.id: d for d in DEFECTS}
CHECKS: dict = {fid: tuple(items) for fid, items in _RULE_CHECKS.items()}
for _defect, _fids in REPRODUCED_BY.items():
    for _fid in _fids:
        CHECKS[_fid] = CHECKS.get(_fid, ()) + (_DEFECT[_defect],)
