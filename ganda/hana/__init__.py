from ganda.hana.provider import PROVIDER

# a `Schema` bound to SAP HANA, for hand-built schemas
Schema = PROVIDER.schema

__all__ = ["PROVIDER", "Schema"]
