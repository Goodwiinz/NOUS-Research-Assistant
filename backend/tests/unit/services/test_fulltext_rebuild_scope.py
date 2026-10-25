"""Tenant-scoping tests for full-text index maintenance."""

from unittest.mock import MagicMock
from uuid import uuid4

from src.models.document import ProcessingStatus
from src.services.search.fulltext_search_service import FullTextSearchService


def test_rebuild_documents_are_filtered_to_the_requested_organization() -> None:
    organization_id = uuid4()
    db = MagicMock()
    query = db.query.return_value
    query.filter.return_value = query
    query.all.return_value = []

    service = FullTextSearchService()
    service.get_rebuild_documents(organization_id, db)

    predicates = query.filter.call_args.args
    predicates_by_column = {
        predicate.left.name: predicate
        for predicate in predicates
        if getattr(getattr(predicate, "left", None), "name", None)
    }
    assert set(predicates_by_column) == {
        "organization_id",
        "processing_status",
        "is_deleted",
    }
    assert predicates_by_column["organization_id"].right.value == organization_id
    assert (
        predicates_by_column["processing_status"].right.value
        == ProcessingStatus.COMPLETED
    )
    assert str(predicates_by_column["is_deleted"].right) == "false"
