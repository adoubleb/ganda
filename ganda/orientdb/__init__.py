from ganda.orientdb.provider import PROVIDER

# a `Schema` bound to OrientDB, for hand-built schemas
Schema = PROVIDER.schema

__all__ = ["PROVIDER", "Schema"]
