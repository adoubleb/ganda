"""Guards for the SurrealDB defects (`F<n>`, the legacy port's numbering): the provider's
`known_bugs` slot, one module per IR (`traversal`, `select`, `write`) over the guards on
one predicate (`predicate`).

Each guard recognises the exact query shape that triggers one defect and nothing
wider. When a concrete syntax would only re-report it, the case is marked
`case.known_bugs[data_model] = "<id>"`; the runner then withholds that data model and
counts the suppression instead of writing a bundle. `--no-known-bugs` turns the guards
off, which is how a filed finding is re-confirmed.

On 3.3.4 the legacy index defects F4, F8, F19, F20 and F27 no longer reproduce
(`rules/probe.py`), and F6 (a bounded shortest path) and F21 (an index defined in a
transaction) are avoided by design (`works_around`): no guard withholds a variant yet.
Each defect is described in `issue-drafts/<id>-*.md` and in `ganda/surrealdb/rules`.
"""
from ganda.surrealdb.known_bugs.select import select_known
from ganda.surrealdb.known_bugs.traversal import mark_known_bugs
from ganda.surrealdb.known_bugs.write import write_known

__all__ = ["mark_known_bugs", "select_known", "write_known"]
