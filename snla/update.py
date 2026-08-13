"""Manual, metadata-minimal update checks against the StatsTalk GitHub release feed."""

from __future__ import annotations

import json
import platform
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit

from snla.llm.transport import NoRedirectHandler
from snla.version import APP_VERSION

RELEASE_API_URL = "https://api.github.com/repos/NorocosA/StatsTalk/releases/latest"
RELEASE_PAGE_PREFIX = "https://github.com/NorocosA/StatsTalk/releases/"
MAX_RESPONSE_BYTES = 64 * 1024


class UpdateCheckError(RuntimeError):
    """The fixed release endpoint did not return a safe, usable response."""


@dataclass(frozen=True)
class UpdateStatus:
    current_version: str
    latest_version: str
    update_available: bool
    release_url: str

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "current_version": self.current_version,
            "latest_version": self.latest_version,
            "update_available": self.update_available,
            "release_url": self.release_url,
        }


def _version_key(value: str) -> tuple[int, int, int, int, str]:
    normalized = value.strip().removeprefix("v")
    core, separator, prerelease = normalized.partition("-")
    parts = core.split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        raise UpdateCheckError("The release feed returned an invalid version.")
    major, minor, patch = (int(part) for part in parts)
    return major, minor, patch, 0 if separator else 1, prerelease


def _windows_label() -> str:
    return f"Windows {platform.release()} {platform.version()}".strip()


def _safe_release_url(value: object) -> str:
    if not isinstance(value, str) or not value.startswith(RELEASE_PAGE_PREFIX):
        raise UpdateCheckError("The release feed returned an invalid release link.")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise UpdateCheckError("The release feed returned an invalid release link.")
    return value


def check_for_update(*, opener=None, os_label: str | None = None) -> UpdateStatus:
    """Check the fixed GitHub feed after an explicit user action; never download a release."""

    operating_system = os_label or _windows_label()
    request = urllib.request.Request(
        RELEASE_API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"StatsTalk/{APP_VERSION} ({operating_system})",
            "X-StatsTalk-Version": APP_VERSION,
            "X-StatsTalk-OS": operating_system,
        },
    )
    if opener is None:
        opener = urllib.request.build_opener(
            NoRedirectHandler(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )
    try:
        with opener.open(request, timeout=10) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateCheckError("The release check could not reach GitHub securely.") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise UpdateCheckError("The release feed response was unexpectedly large.")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateCheckError("The release feed returned invalid data.") from exc

    latest = str(payload.get("tag_name", "")).strip().removeprefix("v")
    release_url = _safe_release_url(payload.get("html_url"))
    return UpdateStatus(
        current_version=APP_VERSION,
        latest_version=latest,
        update_available=_version_key(latest) > _version_key(APP_VERSION),
        release_url=release_url,
    )


__all__ = ["UpdateCheckError", "UpdateStatus", "check_for_update"]
