from ganda.arangodb.provider import PROVIDER

# a `Schema` bound to ArangoDB, for hand-built schemas
Schema = PROVIDER.schema

__all__ = ["PROVIDER", "Schema"]
