"""Audit I25: ``init_database`` must never print generated seed passwords.

Seed output lands in container logs, CI logs and shell scrollback, so an
auto-generated admin/demo/lab-admin password printed there is a leaked
credential. Runs the real seed path against in-memory SQLite.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.core import database
from src.models import encrypted_fields
from src.models.organization import Organization
from src.models.user import User

_SEED_PASSWORD_NAMES = (
    "SEED_ADMIN_PASSWORD",
    "SEED_DEMO_PASSWORD",
    "SEED_LAB_ADMIN_PASSWORD",
)


@pytest.fixture
def seed_engine(monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    # Mirror the module engines' hide_parameters so a regression there shows up here.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        hide_parameters=database.engine.hide_parameters,
    )
    Organization.__table__.create(engine)
    User.__table__.create(engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
    # Column encryption is orthogonal to I25; store plaintext in the test DB.
    monkeypatch.setattr(
        encrypted_fields, "encrypt_sensitive_field", lambda value, _field: value
    )
    for name in _SEED_PASSWORD_NAMES:
        monkeypatch.setattr(database, name, "")
    yield engine
    engine.dispose()


@pytest.mark.unit
def test_init_database_does_not_print_generated_passwords(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    seed_engine: object,
) -> None:
    generated: list[str] = []
    real_set_password = User.set_password

    def spy(self: User, password: str) -> None:
        generated.append(password)
        real_set_password(self, password)

    monkeypatch.setattr(User, "set_password", spy)

    with caplog.at_level(logging.DEBUG):
        database.init_database()

    assert len(generated) == 3  # admin, demo, lab-admin were all generated
    out = capsys.readouterr()
    emitted = out.out + out.err + caplog.text
    assert "admin@multimodal-rag.com" in emitted  # emails are still reported
    for password in generated:
        assert password not in emitted


@pytest.mark.unit
def test_seed_failure_does_not_leak_password_hash(
    capsys: pytest.CaptureFixture[str], seed_engine: object
) -> None:
    # A pre-existing admin row (no organization yet) makes the admin INSERT
    # violate the unique email constraint after the password was hashed.
    session = database.SessionLocal()
    session.add(
        User(
            email="admin@multimodal-rag.com",
            password_hash="pre-existing",
            first_name="Pre",
            last_name="Existing",
            organization_id=None,
        )
    )
    session.commit()
    session.close()

    with pytest.raises(IntegrityError) as excinfo:
        database.init_database()

    out = capsys.readouterr()
    for text in (str(excinfo.value), out.out, out.err):
        assert "$2b$" not in text  # bcrypt hash of the generated password
        assert "[parameters:" not in text


@pytest.mark.unit
def test_module_engines_hide_bound_parameters() -> None:
    assert database.engine.hide_parameters
    assert database.async_engine.sync_engine.hide_parameters
