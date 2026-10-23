from ganda.core.rules.model import Catalog
from ganda.hana.rules.catalog import DEFECTS, ERRORS, RULES, Subject
from ganda.hana.rules.checks import CHECKS

CATALOG = Catalog("hana", Subject, RULES, DEFECTS, ERRORS, CHECKS)
