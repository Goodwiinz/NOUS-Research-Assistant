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


def test_rejected_data_rotation_keeps_fields_decryptable_after_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENCRYPTION_MASTER_KEY", base64.b64encode(b"a" * 32).decode())
    encryption.initialize_encryption()
    manager = encryption.get_key_manager()
    original = manager.get_active_key(encryption.EncryptionKeyType.DATA)
    assert original is not None

    with pytest.raises(encryption.KeyManagementError, match="DATA key rotation"):
        manager.rotate_key(original.key_id)

    assert original.is_active
    assert manager.get_active_key(encryption.EncryptionKeyType.DATA) is original
    field = EncryptedString().copy(field_name="first_name")
    stored = field.process_bind_param("Alice after rejected rotation", None)
    encryption.initialize_encryption()
    assert field.process_result_value(stored, None) == "Alice after rejected rotation"


def test_plaintext_legacy_value_logs_no_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A legacy/plaintext row is the documented graceful fallback (returned
    raw, DEBUG only). FieldEncryption.decrypt_field used to log at ERROR before
    raising, so every load of such a row -- MultiTenancyMiddleware loads the
    user on each request -- sent an error to Sentry (JAVASCRIPT-NEXTJS-5Q,
    ~200k/day from one polling browser, exhausting the error quota)."""
    monkeypatch.setenv("ENCRYPTION_MASTER_KEY", base64.b64encode(b"a" * 32).decode())
    encryption.initialize_encryption()
    field = EncryptedString().copy(field_name="first_name")

    with caplog.at_level("DEBUG"):
        assert field.process_result_value("Alice", None) == "Alice"

    assert [r for r in caplog.records if r.levelname == "ERROR"] == []


def test_real_ciphertext_failure_still_logs_one_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    field = EncryptedString().copy(field_name="first_name")
    monkeypatch.setenv("ENCRYPTION_MASTER_KEY", base64.b64encode(b"a" * 32).decode())
    encryption.initialize_encryption()
    stored = field.process_bind_param("Alice", None)
    monkeypatch.setenv("ENCRYPTION_MASTER_KEY", base64.b64encode(b"b" * 32).decode())
    encryption.initialize_encryption()

    with caplog.at_level("DEBUG"):
        assert field.process_result_value(stored, None) is None

    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert "first_name" in errors[0].getMessage()
