from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from curl_cffi.requests.exceptions import ConnectionError, RequestException, Timeout

from mdcx.base.file import move_file_to_failed_folder
from mdcx.config.manager import manager
from mdcx.config.models import Config, DownloadGuardConfig
from mdcx.controllers.main_window.main_window import MyMAinWindow
from mdcx.web_async import AsyncWebClient


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [ConnectionError("offline"), Timeout("timeout"), RequestException("reset", code=56)])
async def test_transient_network_errors_retry(failure):
    client = AsyncWebClient(timeout=1, retry=3)
    response = SimpleNamespace(status_code=200, headers={})
    client.curl_session.request = AsyncMock(side_effect=[failure, response])
    with patch("mdcx.web_async.asyncio.sleep", new_callable=AsyncMock):
        result, error = await client.request("GET", "https://example.test/image.jpg")
    assert result is response and not error
    assert client.curl_session.request.await_count == 2
    await client.curl_session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,attempts", [(503, 3), (404, 1)])
async def test_http_retry_policy(status, attempts):
    client = AsyncWebClient(timeout=1, retry=3)
    client.curl_session.request = AsyncMock(return_value=SimpleNamespace(status_code=status, headers={}))
    with patch("mdcx.web_async.asyncio.sleep", new_callable=AsyncMock):
        result, error = await client.request("GET", "https://example.test/image.jpg")
    assert result is None and f"HTTP {status}" in error
    assert client.curl_session.request.await_count == attempts
    await client.curl_session.close()


@pytest.mark.asyncio
async def test_images_skip_head_and_report_failure(tmp_path):
    client = AsyncWebClient(timeout=1, retry=3)
    client.get_filesize = AsyncMock()
    client.get_content = AsyncMock(return_value=(None, "network error"))
    errors = []
    assert not await client.download("https://example.test/image", tmp_path / "poster.jpg", on_error=errors.append)
    client.get_filesize.assert_not_awaited()
    assert errors == ["network error"]
    await client.curl_session.close()


@pytest.mark.asyncio
async def test_guard_preserves_failed_source(tmp_path, monkeypatch):
    monkeypatch.setattr(manager, "config", Config(download_guard=DownloadGuardConfig(enabled=True)))
    original = tmp_path / "movie.mp4"
    original.write_bytes(b"video")
    assert await move_file_to_failed_folder(tmp_path / "failed", original, tmp_path) == original
    assert original.read_bytes() == b"video"
    assert not (tmp_path / "failed").exists()


def test_old_cover_result_is_discarded():
    window = SimpleNamespace(_pic_generation=2, Ui=SimpleNamespace(label_poster_size=Mock()))
    MyMAinWindow.resize_label_and_setpixmap(window, 1, [], [], "old")
    window.Ui.label_poster_size.setText.assert_not_called()
