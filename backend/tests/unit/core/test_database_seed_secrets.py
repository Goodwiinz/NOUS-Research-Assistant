"""Audit I25: ``init_database`` must never print generated seed passwords.

Seed output lands in container logs, CI logs and shell scrollback, so an
auto-generated admin/demo/lab-admin password printed there is a leaked
credential. Runs the real seed path against in-memory SQLite.
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.core import database
from src.models import encrypted_fields
from src.models.organization import Organization
from src.models.user import User


@pytest.mark.unit
def test_init_database_does_not_print_generated_passwords(monkeypatch, capsys, caplog):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Organization.__table__.create(engine)
    User.__table__.create(engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
    # Column encryption is orthogonal to I25; store plaintext in the test DB.
    monkeypatch.setattr(
        encrypted_fields, "encrypt_sensitive_field", lambda value, _field: value
    )
    for name in (
        "SEED_ADMIN_PASSWORD",
        "SEED_DEMO_PASSWORD",
        "SEED_LAB_ADMIN_PASSWORD",
    ):
        monkeypatch.setattr(database, name, "")

    generated: list[str] = []
    real_set_password = User.set_password

    def spy(self, password):
        generated.append(password)
        return real_set_password(self, password)

    monkeypatch.setattr(User, "set_password", spy)

    with caplog.at_level(logging.DEBUG):
        database.init_database()

    assert len(generated) == 3  # admin, demo, lab-admin were all generated
    out = capsys.readouterr()
    emitted = out.out + out.err + caplog.text
    assert "admin@multimodal-rag.com" in emitted  # emails are still reported
    for password in generated:
        assert password not in emitted
