"""The key used by encrypted user fields must survive API worker restarts."""

import base64
import json
from collections.abc import Generator

import pytest

from src.core import encryption
from src.models.encrypted_fields import EncryptedString


@pytest.fixture(autouse=True)
def restore_encryption_globals() -> Generator[None, None, None]:
    names = ("_key_manager", "_aes_encryption", "_field_encryption", "_file_encryption")
    previous = {name: getattr(encryption, name) for name in names}
    yield
    for name, value in previous.items():
        setattr(encryption, name, value)


def test_encrypted_user_name_survives_reinitialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENCRYPTION_MASTER_KEY", base64.b64encode(b"a" * 32).decode())
    field = EncryptedString().copy(field_name="first_name")

    encryption.initialize_encryption()
    stored = field.process_bind_param("Alice", None)
    assert json.loads(stored)["key_id"] == "master-derived-field-data-v1"

    encryption.initialize_encryption()
    assert field.process_result_value(stored, None) == "Alice"


def test_different_master_key_cannot_decrypt_existing_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    field = EncryptedString().copy(field_name="first_name")
    monkeypatch.setenv("ENCRYPTION_MASTER_KEY", base64.b64encode(b"a" * 32).decode())
    encryption.initialize_encryption()
    stored = field.process_bind_param("Alice", None)

    monkeypatch.setenv("ENCRYPTION_MASTER_KEY", base64.b64encode(b"b" * 32).decode())
    encryption.initialize_encryption()
    assert field.process_result_value(stored, None) is None
