"""Separated Medallia Query API (GraphQL) client package."""

from client.medallia_client import (
    MedalliaClient,
    MedalliaClientError,
    MedalliaQueryBuilder,
    MedalliaTokenManager,
    Watermark,
    flatten_node,
    watermark_from_node,
)

__all__ = [
    "MedalliaClient",
    "MedalliaClientError",
    "MedalliaQueryBuilder",
    "MedalliaTokenManager",
    "Watermark",
    "flatten_node",
    "watermark_from_node",
]
