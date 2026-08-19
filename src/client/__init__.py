"""Separated generic Medallia Query API (GraphQL) client package."""

from .medallia_client import (
    METADATA_CATALOG_DENYLIST,
    SHAPE_DATA,
    SHAPE_FIELDDATA,
    SHAPE_SCALAR,
    STATIC_EXTRACTABLE_OBJECTS,
    STATIC_OBJECT_SHAPES,
    GenericQueryBuilder,
    MedalliaClient,
    MedalliaClientError,
    MedalliaTokenManager,
    ObjectShape,
    flatten_node,
    list_extractable_objects,
    resolve_object_shape,
    row_hash,
)

__all__ = [
    "METADATA_CATALOG_DENYLIST",
    "SHAPE_DATA",
    "SHAPE_FIELDDATA",
    "SHAPE_SCALAR",
    "STATIC_EXTRACTABLE_OBJECTS",
    "STATIC_OBJECT_SHAPES",
    "GenericQueryBuilder",
    "MedalliaClient",
    "MedalliaClientError",
    "MedalliaTokenManager",
    "ObjectShape",
    "flatten_node",
    "list_extractable_objects",
    "resolve_object_shape",
    "row_hash",
]
