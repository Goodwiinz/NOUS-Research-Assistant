"""S3 client selection preserves Spaces while supporting EKS IAM credentials."""

from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from src.core import s3_client

pytestmark = pytest.mark.unit


def test_explicit_keys_required_only_for_custom_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(s3_client.settings, "S3_ENDPOINT_URL", None)
    monkeypatch.setattr(s3_client.settings, "S3_ACCESS_KEY", None)
    monkeypatch.setattr(s3_client.settings, "S3_SECRET_KEY", None)
    assert s3_client.missing_s3_credentials() == []

    monkeypatch.setattr(s3_client.settings, "S3_ENDPOINT_URL", "https://spaces.example")
    assert s3_client.missing_s3_credentials() == ["S3_ACCESS_KEY", "S3_SECRET_KEY"]


def test_aws_s3_uses_iam_and_omits_acls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(s3_client, "_s3_client", None)
    monkeypatch.setattr(s3_client.settings, "S3_ENDPOINT_URL", None)
    monkeypatch.setattr(s3_client.settings, "S3_ACCESS_KEY", "unused-spaces-key")
    monkeypatch.setattr(s3_client.settings, "S3_SECRET_KEY", "unused-spaces-secret")
    monkeypatch.setattr(s3_client.settings, "S3_REGION", "us-east-1")
    monkeypatch.setattr(s3_client.settings, "S3_BUCKET_NAME", "aws-bucket")
    client = MagicMock()

    with patch("boto3.client", return_value=client) as create_client:
        helper = s3_client.S3StorageHelper()

    assert create_client.call_args.args == ("s3",)
    assert create_client.call_args.kwargs["region_name"] == "us-east-1"
    assert "endpoint_url" not in create_client.call_args.kwargs
    assert "aws_access_key_id" not in create_client.call_args.kwargs
    assert "aws_secret_access_key" not in create_client.call_args.kwargs

    fileobj = BytesIO(b"data")
    helper.upload_file("one", b"data", "text/plain")
    helper.upload_fileobj(fileobj, "two", "text/plain")
    assert client.put_object.call_count == 2
    assert all("ACL" not in call.kwargs for call in client.put_object.call_args_list)


def test_spaces_keeps_explicit_credentials_and_private_acl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(s3_client, "_s3_client", None)
    monkeypatch.setattr(
        s3_client.settings, "S3_ENDPOINT_URL", "https://nyc3.digitaloceanspaces.com"
    )
    monkeypatch.setattr(s3_client.settings, "S3_ACCESS_KEY", "test-key")
    monkeypatch.setattr(s3_client.settings, "S3_SECRET_KEY", "test-secret")
    monkeypatch.setattr(s3_client.settings, "S3_BUCKET_NAME", "spaces-bucket")
    client = MagicMock()

    with patch("boto3.client", return_value=client) as create_client:
        helper = s3_client.S3StorageHelper()

    assert create_client.call_args.kwargs["endpoint_url"] == (
        "https://nyc3.digitaloceanspaces.com"
    )
    assert create_client.call_args.kwargs["aws_access_key_id"] == "test-key"
    assert create_client.call_args.kwargs["aws_secret_access_key"] == "test-secret"

    helper.upload_file("one", b"data", "text/plain")
    assert client.put_object.call_args.kwargs["ACL"] == "private"
