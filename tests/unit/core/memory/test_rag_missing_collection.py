"""Tests for graceful handling of missing ChromaDB collections.

Verifies that query and other read operations on non-existent collections
return empty results instead of raising exceptions, while existing
collections continue to work normally.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from core.memory.rag.store import ChromaVectorStore, Document, SearchResult


class _FakeStore(ChromaVectorStore):
    """Bypass real Chroma init; inject a mock client."""

    def __init__(self, client: MagicMock) -> None:
        self.client = client
        self.persist_dir = Path("/tmp/vectordb")
        self.anima_name = "test"
        self._closed = False


def _make_missing_collection_error(name: str) -> Exception:
    """Simulate chromadb.errors.InvalidCollectionException / ValueError."""
    return ValueError(f"Collection {name} does not exist.")


def _mock_client_with_collections(*existing: str) -> MagicMock:
    """Create a mock Chroma client where only *existing* collections exist."""
    client = MagicMock()

    def get_collection(name: str) -> MagicMock:
        if name not in existing:
            raise _make_missing_collection_error(name)
        coll = MagicMock()
        coll.query.return_value = {
            "ids": [["doc1"]],
            "documents": [["test content"]],
            "metadatas": [[{"scope": "knowledge"}]],
            "distances": [[0.1]],
        }
        coll.get.return_value = {
            "ids": ["doc1"],
            "documents": ["test content"],
            "metadatas": [{"scope": "knowledge"}],
        }
        return coll

    client.get_collection.side_effect = get_collection
    client.get_or_create_collection.return_value = MagicMock()
    return client


@pytest.fixture()
def dummy_embedding() -> list[float]:
    return [0.1] * 384


class TestMissingCollectionQuery:
    """query / _query_once on missing collections must return []."""

    def test_query_once_missing_returns_empty(self, dummy_embedding: list[float]) -> None:
        client = _mock_client_with_collections()  # no collections exist
        store = _FakeStore(client)
        results = store._query_once("nonexistent_facts", dummy_embedding, top_k=5)
        assert results == []

    def test_query_missing_returns_empty(self, dummy_embedding: list[float]) -> None:
        """The public query() method also returns [] for missing collections."""
        client = _mock_client_with_collections()
        store = _FakeStore(client)
        results = store.query("nonexistent_facts", dummy_embedding, top_k=5)
        assert results == []

    def test_query_existing_returns_results(self, dummy_embedding: list[float]) -> None:
        client = _mock_client_with_collections("real_knowledge")
        store = _FakeStore(client)
        results = store._query_once("real_knowledge", dummy_embedding, top_k=5)
        assert len(results) == 1
        assert results[0].document.id == "doc1"

    def test_missing_does_not_break_subsequent_existing(self, dummy_embedding: list[float]) -> None:
        """Querying a missing collection first must not prevent subsequent
        queries on existing collections."""
        client = _mock_client_with_collections("real_collection")
        store = _FakeStore(client)

        missing = store._query_once("nonexistent_facts", dummy_embedding)
        assert missing == []

        existing = store._query_once("real_collection", dummy_embedding)
        assert len(existing) == 1


class TestMissingCollectionReadOps:
    """Other read operations must also tolerate missing collections."""

    def test_get_by_ids_once_missing(self) -> None:
        client = _mock_client_with_collections()
        store = _FakeStore(client)
        results = store._get_by_ids_once("nonexistent", ["id1"])
        assert results == []

    def test_get_by_ids_missing(self) -> None:
        client = _mock_client_with_collections()
        store = _FakeStore(client)
        results = store.get_by_ids("nonexistent", ["id1"])
        assert results == []

    def test_get_by_metadata_once_missing(self) -> None:
        """_get_by_metadata_once already has handling (pre-existing)."""
        client = _mock_client_with_collections()
        store = _FakeStore(client)
        results = store._get_by_metadata_once("nonexistent", {"scope": "facts"})
        assert results == []


class TestMissingCollectionWriteOps:
    """Write operations on missing collections."""

    def test_delete_documents_once_missing(self) -> None:
        """Deleting from non-existent collection is a no-op success."""
        client = _mock_client_with_collections()
        store = _FakeStore(client)
        result = store._delete_documents_once("nonexistent", ["id1"])
        assert result is True

    def test_update_metadata_once_missing(self) -> None:
        """Updating metadata on non-existent collection returns False."""
        client = _mock_client_with_collections()
        store = _FakeStore(client)
        result = store._update_metadata_once("nonexistent", ["id1"], [{"key": "val"}])
        assert result is False
