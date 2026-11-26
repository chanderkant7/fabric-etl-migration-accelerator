"""Bounded Microsoft SharePoint folder staging through Microsoft Graph."""

from __future__ import annotations

import base64
import json
import os
import re
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from etl_migration_core.intake.discovery import validate_member_path
from etl_migration_core.intake.domain import DiscoveryLimits
from etl_migration_core.sources.alteryx.graph import redact

_GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
_JSON_RESPONSE_LIMIT = 4 * 1024 * 1024
_HTTP_ERROR_BODY_LIMIT = 8192
_HTTP_ERROR_MESSAGE_LIMIT = 512
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
_TENANT_PATTERN = re.compile(r"^[A-Za-z0-9.-]+$")


class SharePointIntakeError(ValueError):
    """Report a safe, actionable SharePoint intake failure."""


class _HttpResponse(Protocol):
    status: int

    def read(self, amount: int = -1) -> bytes: ...

    def __enter__(self) -> _HttpResponse: ...

    def __exit__(self, *args: object) -> None: ...


@dataclass(frozen=True, slots=True)
class SharePointCredentials:
    """Microsoft Entra application credentials used for Graph folder reads."""

    tenant_id: str
    client_id: str
    client_secret: str

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> SharePointCredentials:
        """Read the existing Microsoft Entra service-principal settings."""
        env = os.environ if environ is None else environ
        values = {
            "AZURE_TENANT_ID": (env.get("AZURE_TENANT_ID") or "").strip(),
            "AZURE_CLIENT_ID": (env.get("AZURE_CLIENT_ID") or "").strip(),
            "AZURE_CLIENT_SECRET": (env.get("AZURE_CLIENT_SECRET") or "").strip(),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise SharePointIntakeError(
                "SharePoint intake requires server-side "
                + ", ".join(missing)
                + " configuration."
            )
        if not _TENANT_PATTERN.fullmatch(values["AZURE_TENANT_ID"]):
            raise SharePointIntakeError("AZURE_TENANT_ID is invalid.")
        return cls(
            tenant_id=values["AZURE_TENANT_ID"],
            client_id=values["AZURE_CLIENT_ID"],
            client_secret=values["AZURE_CLIENT_SECRET"],
        )


def stage_sharepoint_folder(
    folder_url: str,
    sandbox: Path,
    *,
    environ: Mapping[str, str] | None = None,
    limits: DiscoveryLimits | None = None,
) -> list[Path]:
    """Download a SharePoint folder tree into a bounded private sandbox."""
    normalized_url = _validate_sharepoint_url(folder_url)
    credentials = SharePointCredentials.from_environment(environ)
    token = _acquire_graph_token(credentials)
    bounds = limits or DiscoveryLimits()
    root = _graph_json(
        f"{_GRAPH_ROOT}/shares/{_share_id(normalized_url)}/driveItem"
        "?$select=id,name,folder,parentReference,remoteItem",
        token,
    )
    root_item = _remote_item(root)
    root_id = _required_text(root_item, "id", "SharePoint did not return the folder ID.")
    if not isinstance(root_item.get("folder"), dict):
        raise SharePointIntakeError("The SharePoint URL must identify a folder.")
    drive_id = _drive_id(root_item)

    sandbox.mkdir(parents=True, exist_ok=True)
    queue: deque[tuple[str, PurePosixPath]] = deque([(root_id, PurePosixPath())])
    staged: list[Path] = []
    seen_paths: set[str] = set()
    entry_count = 0
    total_size = 0

    while queue:
        folder_id, relative_folder = queue.popleft()
        next_url: str | None = (
            f"{_GRAPH_ROOT}/drives/{quote(drive_id, safe='')}/items/"
            f"{quote(folder_id, safe='')}/children"
            "?$select=id,name,size,file,folder,parentReference,remoteItem,"
            "%40microsoft.graph.downloadUrl"
        )
        while next_url:
            page = _graph_json(_validate_graph_url(next_url), token)
            items = page.get("value")
            if not isinstance(items, list):
                raise SharePointIntakeError("Microsoft Graph returned an invalid folder listing.")
            for raw_item in items:
                if not isinstance(raw_item, dict):
                    raise SharePointIntakeError(
                        "Microsoft Graph returned an invalid folder item."
                    )
                item = _remote_item(raw_item)
                name = _required_text(
                    item, "name", "Microsoft Graph returned a folder item without a name."
                )
                relative_path = validate_member_path(
                    Path("sharepoint"), str(relative_folder / name)
                )
                key = str(relative_path).casefold()
                if key in seen_paths:
                    raise SharePointIntakeError(
                        f"SharePoint folder contains colliding path: {relative_path}"
                    )
                seen_paths.add(key)
                entry_count += 1
                if entry_count > bounds.max_archive_entries:
                    raise SharePointIntakeError(
                        f"SharePoint folder exceeds the {bounds.max_archive_entries} entry limit."
                    )

                if isinstance(item.get("folder"), dict):
                    child_id = _required_text(
                        item, "id", "Microsoft Graph returned a folder without an ID."
                    )
                    queue.append((child_id, relative_path))
                    continue
                if not isinstance(item.get("file"), dict):
                    continue

                declared_size = item.get("size")
                if (
                    not isinstance(declared_size, int)
                    or isinstance(declared_size, bool)
                    or declared_size < 0
                ):
                    raise SharePointIntakeError(
                        f"Microsoft Graph returned an invalid size for {relative_path}."
                    )
                if declared_size > bounds.max_file_bytes:
                    raise SharePointIntakeError(
                        f"{relative_path} exceeds the {bounds.max_file_bytes} byte file limit."
                    )
                if total_size + declared_size > bounds.max_archive_uncompressed_bytes:
                    raise SharePointIntakeError(
                        "SharePoint folder exceeds the aggregate intake size limit."
                    )
                download_url = _required_text(
                    item,
                    "@microsoft.graph.downloadUrl",
                    f"Microsoft Graph did not provide a download URL for {relative_path}.",
                )
                destination = sandbox.joinpath(*relative_path.parts)
                actual_size = _download_file(
                    download_url,
                    destination,
                    file_limit=bounds.max_file_bytes,
                    remaining_limit=bounds.max_archive_uncompressed_bytes - total_size,
                )
                total_size += actual_size
                staged.append(destination)

            next_value = page.get("@odata.nextLink")
            if next_value is not None and not isinstance(next_value, str):
                raise SharePointIntakeError("Microsoft Graph returned an invalid paging link.")
            next_url = next_value

    if not staged:
        raise SharePointIntakeError("The SharePoint folder contains no downloadable files.")
    return staged


def _validate_sharepoint_url(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 2048:
        raise SharePointIntakeError("Enter a valid SharePoint folder URL.")
    parsed = urlparse(normalized)
    hostname = (parsed.hostname or "").casefold()
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or not hostname.endswith(".sharepoint.com")
    ):
        raise SharePointIntakeError("Use an HTTPS URL on your SharePoint tenant.")
    return normalized


def _validate_graph_url(value: str) -> str:
    if not value.startswith(f"{_GRAPH_ROOT}/"):
        raise SharePointIntakeError("Microsoft Graph returned an untrusted paging link.")
    return value


def _share_id(folder_url: str) -> str:
    encoded = base64.urlsafe_b64encode(folder_url.encode("utf-8")).decode("ascii").rstrip("=")
    return f"u!{encoded}"


def _acquire_graph_token(credentials: SharePointCredentials) -> str:
    body = urlencode(
        {
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        }
    ).encode("ascii")
    request = Request(
        "https://login.microsoftonline.com/"
        f"{quote(credentials.tenant_id, safe='')}/oauth2/v2.0/token",
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    payload = _request_json(request, "Microsoft Entra authentication failed")
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise SharePointIntakeError("Microsoft Entra did not return an access token.")
    return token


def _graph_json(url: str, token: str) -> dict[str, Any]:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    path = urlparse(url).path
    if "/shares/" in path:
        context = "Microsoft Graph could not resolve the SharePoint sharing link"
    elif path.endswith("/children"):
        context = "Microsoft Graph could not read the SharePoint folder listing"
    else:
        context = "Microsoft Graph could not read the SharePoint folder"
    return _request_json(request, context)


def _http_error_details(error: HTTPError, request: Request) -> str:
    details: list[str] = []
    request_id = error.headers.get("request-id") if error.headers else None
    try:
        content = error.read(_HTTP_ERROR_BODY_LIMIT + 1)
    except (OSError, URLError):
        details.append("Error details could not be read.")
        content = b""
    finally:
        error.close()

    payload: object = None
    if len(content) > _HTTP_ERROR_BODY_LIMIT:
        details.append("Error details exceeded the size limit.")
    elif content:
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            details.append("Error details were not valid JSON.")
    error_info = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error_info, dict):
        code = error_info.get("code")
        if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", code):
            details.append(f"Graph code: {code}.")
        message = error_info.get("message")
        if isinstance(message, str) and message.strip():
            message = re.sub(
                r"https?://[^\s\"'<>]+", "[redacted URL]", message, flags=re.IGNORECASE
            )
            message = re.sub(r"u![A-Za-z0-9_-]+", "[redacted sharing token]", message)
            authorization = request.get_header("Authorization")
            if isinstance(authorization, str) and authorization.startswith("Bearer "):
                token = authorization.removeprefix("Bearer ")
                if token:
                    message = message.replace(token, "[redacted]")
            message = redact(message)
            message = " ".join(message.split())
            details.append(message[:_HTTP_ERROR_MESSAGE_LIMIT])
        inner_error = error_info.get("innerError", error_info.get("innererror"))
        if not request_id and isinstance(inner_error, dict):
            request_id = inner_error.get("request-id")
    elif content and not details:
        details.append("No structured error details were returned.")
    if isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9-]{1,100}", request_id):
        details.append(f"Request ID: {request_id}.")
    return " ".join(details)


def _request_json(request: Request, context: str) -> dict[str, Any]:
    try:
        with _open(request, timeout=30) as response:
            content = response.read(_JSON_RESPONSE_LIMIT + 1)
    except HTTPError as error:
        detail = _http_error_details(error, request)
        raise SharePointIntakeError(f"{context}: HTTP {error.code}. {detail}".rstrip()) from error
    except TimeoutError as error:
        raise SharePointIntakeError(f"{context}: request timed out.") from error
    except URLError as error:
        raise SharePointIntakeError(f"{context}: {error.reason}.") from error
    if len(content) > _JSON_RESPONSE_LIMIT:
        raise SharePointIntakeError(f"{context}: response exceeded the size limit.")
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SharePointIntakeError(f"{context}: invalid JSON response.") from error
    if not isinstance(payload, dict):
        raise SharePointIntakeError(f"{context}: invalid response shape.")
    return cast(dict[str, Any], payload)


def _download_file(
    download_url: str,
    destination: Path,
    *,
    file_limit: int,
    remaining_limit: int,
) -> int:
    parsed = urlparse(download_url)
    hostname = (parsed.hostname or "").casefold()
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or not hostname.endswith(".sharepoint.com")
    ):
        raise SharePointIntakeError("Microsoft Graph returned an untrusted download URL.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.partial")
    size = 0
    try:
        request = Request(download_url, headers={"Accept": "application/octet-stream"})
        with _open(request, timeout=60) as response, temporary.open("wb") as target:
            while chunk := response.read(_DOWNLOAD_CHUNK_BYTES):
                size += len(chunk)
                if size > file_limit or size > remaining_limit:
                    raise SharePointIntakeError(
                        f"{destination.name} exceeds the SharePoint intake size limit."
                    )
                target.write(chunk)
        temporary.replace(destination)
    except HTTPError as error:
        raise SharePointIntakeError(
            f"Microsoft Graph could not download {destination.name}: HTTP {error.code}."
        ) from error
    except TimeoutError as error:
        raise SharePointIntakeError(
            f"Microsoft Graph could not download {destination.name}: request timed out."
        ) from error
    except URLError as error:
        raise SharePointIntakeError(
            f"Microsoft Graph could not download {destination.name}: {error.reason}."
        ) from error
    finally:
        temporary.unlink(missing_ok=True)
    return size


def _remote_item(item: dict[str, Any]) -> dict[str, Any]:
    remote = item.get("remoteItem")
    return cast(dict[str, Any], remote) if isinstance(remote, dict) else item


def _drive_id(item: dict[str, Any]) -> str:
    parent = item.get("parentReference")
    if not isinstance(parent, dict):
        raise SharePointIntakeError("Microsoft Graph did not return the document library ID.")
    return _required_text(
        cast(dict[str, Any], parent),
        "driveId",
        "Microsoft Graph did not return the document library ID.",
    )


def _required_text(item: dict[str, Any], key: str, message: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise SharePointIntakeError(message)
    return value


def _open(request: Request, *, timeout: float) -> _HttpResponse:
    return cast(_HttpResponse, urlopen(request, timeout=timeout))


__all__ = [
    "SharePointCredentials",
    "SharePointIntakeError",
    "stage_sharepoint_folder",
]
