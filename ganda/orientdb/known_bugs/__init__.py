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
"""
from ganda.orientdb.known_bugs.select import select_known
from ganda.orientdb.known_bugs.traversal import mark_known_bugs
from ganda.orientdb.known_bugs.write import write_known

__all__ = ["mark_known_bugs", "select_known", "write_known"]
