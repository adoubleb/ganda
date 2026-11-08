"""Guards for the OrientDB defects already filed (O1, O2, O7, ...): the provider's
`known_bugs` slot, one module per IR (`traversal`, `select`, `write`) over the guards
on one predicate (`predicate`).

Each guard recognises the exact query shape that triggers one defect and nothing
wider. When a concrete syntax would only re-report it, the case is marked
`case.known_bugs[data_model] = "<id>"`; the runner then withholds that data model and
counts the suppression instead of writing a bundle. `--no-known-bugs` turns the guards
off, which is how a filed finding is re-confirmed.

Predicates are trees, so a guard inspects the leaves: an indexed field inside an `OR`
or under a `NOT` triggers a defect just as a bare one does.

Each defect is described in `issue-drafts/<id>-*.md` and in `ganda/orientdb/rules`;
`works_around("<id>")` ties the guard to that catalog entry.

`activate_fixed_defects(server_version)` lets the runner tell this module which
defects the baseline recorded as fixed on the server under test. A guard's hit for
one of them is dropped, so a shape that only re-reports a fix is no longer withheld
and a regression nearby is visible again. Called once per campaign from the runner's
build-skew check; defaults to the empty set, which is today's conservative behaviour.
"""
from ganda.orientdb.known_bugs.select import select_known
from ganda.orientdb.known_bugs.traversal import mark_known_bugs
from ganda.orientdb.known_bugs.write import write_known

__all__ = ["mark_known_bugs", "select_known", "write_known", "activate_fixed_defects", "fixed_defects"]


_FIXED: "frozenset[str]" = frozenset()


def activate_fixed_defects(server_version: "str | None") -> "frozenset[str]":
    """Populate the fixed-defects set from `rules/baseline.json` when the server's build
    matches the baseline's. On a mismatch (or no baseline, or no server version), the
    set stays empty: the runner has already warned, and a guard for a defect fixed on
    the baseline but not on this server must still fire. Returns the set in force."""
    from ganda.orientdb.rules import BASELINE

    base = BASELINE.read()
    global _FIXED
    if not base or server_version is None or base.get("version") != server_version:
        _FIXED = frozenset()
    else:
        _FIXED = frozenset(base.get("fixed", {}) or ())
    return _FIXED


def fixed_defects() -> "frozenset[str]":
    """The defects the baseline recorded as fixed on the server under test (empty until
    `activate_fixed_defects` has been called with a matching build)."""
    return _FIXED


def discard_fixed(fid: "str | None") -> "str | None":
    """None when `fid` names a defect fixed on this build (so the case is not withheld);
    `fid` otherwise."""
    return None if fid is not None and fid in _FIXED else fid
