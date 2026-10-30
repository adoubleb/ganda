"""Guards for the SurrealDB defects (`F<n>`), one module per IR over the predicate guards; `--no-known-bugs` turns them off.
F6 and F21 are avoided by design (`works_around`)."""
from ganda.surrealdb.known_bugs.select import select_known
from ganda.surrealdb.known_bugs.traversal import mark_known_bugs
from ganda.surrealdb.known_bugs.write import write_known

__all__ = ["mark_known_bugs", "select_known", "write_known"]
