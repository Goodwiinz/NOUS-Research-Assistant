"""GOO-314 structural guards: peer review is independent of machine citation
review (``DraftReview``), an external reviewer is never a user, and the
rules stay pure.

Mutation verification: importing ``DraftReview`` into ``peer_review_service``
fails ``-k draft_review``; adding a ``user_id`` column to
``PeerReviewReviewer`` fails ``-k user_id``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from src.models import peer_review as models

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[3] / "src"
PEER_REVIEW_MODULES = (
    SRC / "services/research/peer_review_service.py",
    SRC / "services/research/peer_review_rules.py",
    SRC / "api/research/peer_review.py",
    SRC / "shared/peer_review_schemas.py",
    SRC / "models/peer_review.py",
)
RULES = SRC / "services/research/peer_review_rules.py"


def _imported_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def test_peer_review_never_touches_draft_review() -> None:
    for path in PEER_REVIEW_MODULES:
        names = _imported_names(path)
        assert "DraftReview" not in names, path
        assert "src.models.draft_review" not in names, path


def test_reviewers_have_no_user_id() -> None:
    reviewer_columns = set(models.PeerReviewReviewer.__table__.columns.keys())
    assert not {c for c in reviewer_columns if "user" in c}, reviewer_columns
    # No peer-review table maps anyone through a generic ``user_id`` either.
    model: Any
    for model in (
        models.PeerReviewRound,
        models.PeerReviewComment,
        models.PeerReviewResponse,
        models.PeerReviewDecision,
    ):
        assert "user_id" not in model.__table__.columns.keys(), model


def test_rules_stay_pure() -> None:
    names = _imported_names(RULES)
    assert not {n for n in names if n.startswith(("sqlalchemy", "fastapi"))}, names
