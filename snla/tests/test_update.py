import json
import urllib.error

import pytest

from snla.update import RELEASE_API_URL, UpdateCheckError, check_for_update


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _size):
        return self.payload


class RecordingOpener:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if self.error:
            raise self.error
        return FakeResponse(self.payload)


def _release(tag="0.9.1", url="https://github.com/NorocosA/StatsTalk/releases/tag/0.9.1"):
    return json.dumps({"tag_name": tag, "html_url": url}).encode()


def test_manual_update_check_sends_only_version_and_os_metadata():
    opener = RecordingOpener(_release())

    status = check_for_update(opener=opener, os_label="Windows 11 26100")

    request, timeout = opener.calls[0]
    headers = dict(request.header_items())
    assert request.full_url == RELEASE_API_URL
    assert timeout == 10
    assert headers["X-statstalk-version"] == "0.9.0-beta"
    assert headers["X-statstalk-os"] == "Windows 11 26100"
    assert set(headers) == {"Accept", "User-agent", "X-statstalk-version", "X-statstalk-os"}
    assert status.update_available is True
    assert status.release_url.endswith("/tag/0.9.1")


def test_update_check_never_accepts_a_non_project_download_link():
    opener = RecordingOpener(_release(url="https://example.com/StatsTalk.exe"))

    with pytest.raises(UpdateCheckError, match="invalid release link"):
        check_for_update(opener=opener, os_label="Windows 11")


def test_update_check_rejects_invalid_versions_and_large_responses():
    with pytest.raises(UpdateCheckError, match="invalid version"):
        check_for_update(opener=RecordingOpener(_release(tag="latest")), os_label="Windows 11")
    with pytest.raises(UpdateCheckError, match="unexpectedly large"):
        check_for_update(opener=RecordingOpener(b"x" * 65537), os_label="Windows 11")


def test_update_check_returns_safe_network_error():
    opener = RecordingOpener(error=urllib.error.URLError("C:\\Users\\private\\proxy"))

    with pytest.raises(UpdateCheckError) as raised:
        check_for_update(opener=opener, os_label="Windows 11")

    assert "private" not in str(raised.value)
