"""Tests for bounded Microsoft SharePoint intake staging."""

from __future__ import annotations

import io
import json
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from accelerator.server import sharepoint
from accelerator.server.sharepoint import SharePointIntakeError, stage_sharepoint_folder
from etl_migration_core.intake.domain import DiscoveryLimits


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.status = 200
        self._body = body
        self._offset = 0

    def read(self, amount: int = -1) -> bytes:
        if amount < 0:
            amount = len(self._body) - self._offset
        chunk = self._body[self._offset : self._offset + amount]
        self._offset += len(chunk)
        return chunk

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        del _args
        return None


def _json_response(payload: dict[str, Any]) -> _FakeResponse:
    return _FakeResponse(json.dumps(payload).encode())


def test_stage_sharepoint_folder_downloads_nested_files_with_graph_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph_authorizations: list[str | None] = []
    download_authorizations: list[str | None] = []

    def fake_open(request: Request, *, timeout: float) -> _FakeResponse:
        assert timeout in {30, 60}
        url = request.full_url
        if "login.microsoftonline.com" in url:
            return _json_response({"access_token": "graph-token"})
        if "/shares/" in url:
            graph_authorizations.append(request.get_header("Authorization"))
            return _json_response(
                {
                    "id": "root",
                    "folder": {},
                    "parentReference": {"driveId": "drive"},
                }
            )
        if "/items/root/children" in url:
            graph_authorizations.append(request.get_header("Authorization"))
            return _json_response(
                {
                    "value": [
                        {
                            "id": "nested",
                            "name": "nested",
                            "folder": {},
                            "parentReference": {"driveId": "drive"},
                        },
                        {
                            "id": "workflow",
                            "name": "flow.yxmd",
                            "size": 6,
                            "file": {},
                            "@microsoft.graph.downloadUrl": (
                                "https://contoso.sharepoint.com/download/flow"
                            ),
                        },
                    ]
                }
            )
        if "/items/nested/children" in url:
            graph_authorizations.append(request.get_header("Authorization"))
            return _json_response(
                {
                    "value": [
                        {
                            "id": "macro",
                            "name": "macro.yxmc",
                            "size": 5,
                            "file": {},
                            "@microsoft.graph.downloadUrl": (
                                "https://contoso.sharepoint.com/download/macro"
                            ),
                        }
                    ]
                }
            )
        download_authorizations.append(request.get_header("Authorization"))
        if url.endswith("/flow"):
            return _FakeResponse(b"<flow>")
        if url.endswith("/macro"):
            return _FakeResponse(b"macro")
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(sharepoint, "_open", fake_open)
    staged = stage_sharepoint_folder(
        "https://contoso.sharepoint.com/sites/migrations/Shared%20Documents/exports",
        tmp_path,
        environ={
            "AZURE_TENANT_ID": "tenant.onmicrosoft.com",
            "AZURE_CLIENT_ID": "client-id",
            "AZURE_CLIENT_SECRET": "client-secret",
        },
    )

    assert [path.relative_to(tmp_path).as_posix() for path in staged] == [
        "flow.yxmd",
        "nested/macro.yxmc",
    ]
    assert (tmp_path / "flow.yxmd").read_bytes() == b"<flow>"
    assert (tmp_path / "nested" / "macro.yxmc").read_bytes() == b"macro"
    assert graph_authorizations == ["Bearer graph-token"] * 3
    assert download_authorizations == [None, None]


def test_stage_sharepoint_folder_rejects_untrusted_url_before_authentication(
    tmp_path: Path,
) -> None:
    with pytest.raises(SharePointIntakeError, match="SharePoint tenant"):
        stage_sharepoint_folder(
            "https://example.com/folder",
            tmp_path,
            environ={},
        )


def test_stage_sharepoint_folder_enforces_declared_file_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_open(request: Request, *, timeout: float) -> _FakeResponse:
        del timeout
        url = request.full_url
        if "login.microsoftonline.com" in url:
            return _json_response({"access_token": "graph-token"})
        if "/shares/" in url:
            return _json_response(
                {
                    "id": "root",
                    "folder": {},
                    "parentReference": {"driveId": "drive"},
                }
            )
        if "/children" in url:
            return _json_response(
                {
                    "value": [
                        {
                            "id": "large",
                            "name": "large.yxmd",
                            "size": 11,
                            "file": {},
                            "@microsoft.graph.downloadUrl": (
                                "https://contoso.sharepoint.com/download/large"
                            ),
                        }
                    ]
                }
            )
        raise AssertionError("Oversized content must not be downloaded")

    monkeypatch.setattr(sharepoint, "_open", fake_open)
    with pytest.raises(SharePointIntakeError, match="byte file limit"):
        stage_sharepoint_folder(
            "https://contoso.sharepoint.com/sites/migrations/exports",
            tmp_path,
            environ={
                "AZURE_TENANT_ID": "tenant",
                "AZURE_CLIENT_ID": "client",
                "AZURE_CLIENT_SECRET": "secret",
            },
            limits=DiscoveryLimits(max_file_bytes=10),
        )


@pytest.mark.parametrize("failure_stage", ["sharing-link", "listing"])
def test_stage_sharepoint_folder_reports_safe_graph_error_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_stage: str,
) -> None:
    request_id = "11111111-1111-4111-8111-111111111111"
    sharing_url = "https://contoso.sharepoint.com/:f:/g/synthetic-sharing-token?e=private"
    seen_error: HTTPError | None = None

    def fake_open(request: Request, *, timeout: float) -> _FakeResponse:
        nonlocal seen_error
        del timeout
        url = request.full_url
        if "login.microsoftonline.com" in url:
            return _json_response({"access_token": "synthetic-graph-token"})
        if "/shares/" in url and failure_stage == "listing":
            return _json_response(
                {"id": "root", "folder": {}, "parentReference": {"driveId": "drive"}}
            )
        payload = {
            "error": {
                "code": "invalidRequest",
                "message": (
                    "The sharing link is invalid.\n"
                    f"URL: {sharing_url}; share: {sharepoint._share_id(sharing_url)}; "
                    "Bearer synthetic-graph-token; secret=private-secret"
                ),
                "innerError": {"request-id": request_id},
            }
        }
        seen_error = HTTPError(
            url, 400, "Bad Request", Message(), io.BytesIO(json.dumps(payload).encode())
        )
        raise seen_error

    monkeypatch.setattr(sharepoint, "_open", fake_open)
    with pytest.raises(SharePointIntakeError) as caught:
        stage_sharepoint_folder(
            sharing_url, tmp_path,
            environ={
                "AZURE_TENANT_ID": "tenant",
                "AZURE_CLIENT_ID": "client",
                "AZURE_CLIENT_SECRET": "secret",
            },
        )

    message = str(caught.value)
    assert "HTTP 400" in message
    assert "invalidRequest" in message
    assert "The sharing link is invalid." in message
    assert request_id in message
    expected_stage = "sharing link" if failure_stage == "sharing-link" else "folder listing"
    assert expected_stage in message
    for sensitive_value in (
        sharing_url, sharepoint._share_id(sharing_url),
        "synthetic-graph-token", "private-secret",
    ):
        assert sensitive_value not in message
    assert "\n" not in message
    assert caught.value.__cause__ is seen_error


@pytest.mark.parametrize("body", [b"<html>Bad request</html>", b"[]", b'{"error": 123}'])
def test_graph_http_error_preserves_status_for_unusable_error_body(
    monkeypatch: pytest.MonkeyPatch, body: bytes,
) -> None:
    headers = Message()
    headers["request-id"] = "11111111-1111-4111-8111-111111111111"

    def fake_open(request: Request, *, timeout: float) -> _FakeResponse:
        del timeout
        raise HTTPError(request.full_url, 400, "Bad Request", headers, io.BytesIO(body))

    monkeypatch.setattr(sharepoint, "_open", fake_open)
    with pytest.raises(SharePointIntakeError) as caught:
        sharepoint._graph_json("https://graph.microsoft.com/v1.0/shares/u!test/driveItem", "token")
    assert "HTTP 400" in str(caught.value)
    assert headers["request-id"] in str(caught.value)
    assert "<html>" not in str(caught.value)


def test_graph_http_error_bounds_detail_response_and_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_read_sizes: list[int | None] = []
    message = "Invalid sharing link. " + "x" * sharepoint._HTTP_ERROR_MESSAGE_LIMIT
    payload = json.dumps({"error": {"code": "invalidRequest", "message": message}}).encode()

    class ErrorBody(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            seen_read_sizes.append(size)
            return super().read(size)

    def fake_open(request: Request, *, timeout: float) -> _FakeResponse:
        del timeout
        raise HTTPError(request.full_url, 400, "Bad Request", Message(), ErrorBody(payload))

    monkeypatch.setattr(sharepoint, "_open", fake_open)
    with pytest.raises(SharePointIntakeError) as caught:
        sharepoint._graph_json("https://graph.microsoft.com/v1.0/shares/u!test/driveItem", "token")
    detail = str(caught.value).split("Graph code: invalidRequest. ", 1)[1]
    assert len(detail) == sharepoint._HTTP_ERROR_MESSAGE_LIMIT
    assert seen_read_sizes == [sharepoint._HTTP_ERROR_BODY_LIMIT + 1]

    payload = b"x" * (sharepoint._HTTP_ERROR_BODY_LIMIT + 1)
    with pytest.raises(SharePointIntakeError, match="Error details exceeded the size limit"):
        sharepoint._graph_json("https://graph.microsoft.com/v1.0/shares/u!test/driveItem", "token")
