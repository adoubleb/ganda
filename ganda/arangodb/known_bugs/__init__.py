"""Guards for the ArangoDB defects already filed (A25, A39, A40, A42, A43, A44): the provider's
`known_bugs` slot, one module per IR (`traversal`, `select`, `write`) over the guards
on one predicate (`predicate`).

Each guard recognises the exact query shape that triggers one defect and nothing
wider. When a concrete syntax would only re-report it, the case is marked
`case.known_bugs[data_model] = "<id>"`; the runner then withholds that data model and
counts the suppression instead of writing a bundle. `--no-known-bugs` turns the guards
off, which is how a filed finding is re-confirmed.

The legacy port's A1..A38 (`../arangoDB/issue-drafts/`) are either outside this port's
surface (ArangoSearch views, MDI and geo indexes, the JavaScript door) or kept
unreachable by construction (no signed zero is stored, A1 / A2; no ordered comparison on
a string, A9 / A34); its A39 is reachable and guarded.

Each defect is described in `ganda/arangodb/rules`; `works_around("<id>")` ties the
guard to that catalog entry.
"""
from ganda.arangodb.known_bugs.select import select_known
from ganda.arangodb.known_bugs.traversal import mark_known_bugs
from ganda.arangodb.known_bugs.write import write_known

__all__ = ["mark_known_bugs", "select_known", "write_known"]
