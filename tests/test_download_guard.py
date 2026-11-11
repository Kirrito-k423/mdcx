import os
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from mdcx.config.manager import manager
from mdcx.config.models import Config, DownloadGuardConfig
from mdcx.core.download_guard import DownloadGuard, DownloadNotReady
from mdcx.core.scraper import Scraper
from mdcx.models.enums import FileMode
from mdcx.models.flags import Flags


@pytest.fixture
def download(tmp_path):
    path = tmp_path / "movie" / "part.mp4"
    path.parent.mkdir()
    path.write_bytes(b"x" * 100)  # A preallocated file can already have its final size.
    os.utime(path, (time.time() - 100, time.time() - 100))
    cfg = DownloadGuardConfig(
        enabled=True, url="http://qbit.test", path_mappings={"/downloads": str(tmp_path)}, settle_seconds=30
    )
    task = {
        "hash": "abc",
        "name": "movie",
        "save_path": "/downloads",
        "content_path": "/downloads/movie",
        "state": "pausedUP",
        "progress": 1,
        "completion_on": time.time() - 100,
    }
    file = {"name": "movie/part.mp4", "size": 100, "progress": 1, "priority": 1}
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        if request.url.path.endswith("/torrents/info"):
            return httpx.Response(200, json=[task])
        if request.url.path.endswith("/torrents/files"):
            return httpx.Response(200, json=[file])
        return httpx.Response(404)

    guard = DownloadGuard(cfg, transport=httpx.MockTransport(handler))
    return path, task, file, guard, calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        "downloading",
        "stalledDL",
        "queuedDL",
        "pausedDL",
        "checkingUP",
        "checkingDL",
        "checkingResumeData",
        "missingFiles",
        "moving",
        "error",
        "unknown",
    ],
)
async def test_unsafe_states_never_allow_preallocated_file(download, state):
    path, task, file, guard, calls = download
    task["state"] = state
    task["progress"] = 0.01 if state == "downloading" else 1
    try:
        await guard.prepare([path])
        assert await guard.reason(path)
        assert len(calls) == 1  # No per-file requests or media probes for blocked tasks.
    finally:
        await guard.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["torrent_progress", "file_progress", "size", "priority", "recent_write", "recent_completion"]
)
async def test_completion_requires_all_checks(download, fault):
    path, task, file, guard, calls = download
    if fault == "torrent_progress":
        task["progress"] = 0.99
    if fault == "file_progress":
        file["progress"] = 0.99
    if fault == "size":
        file["size"] = 101
    if fault == "priority":
        file["priority"] = 0
    if fault == "recent_write":
        os.utime(path, None)
    if fault == "recent_completion":
        task["completion_on"] = time.time()
    try:
        await guard.prepare([path])
        assert await guard.reason(path)
    finally:
        await guard.close()


@pytest.mark.asyncio
async def test_complete_file_allowed_but_rechecking_cancels_move(download):
    path, task, file, guard, calls = download
    try:
        await guard.prepare([path])
        assert not await guard.reason(path)
        await guard.ensure_ready(path)
        task["state"] = "checkingUP"
        with pytest.raises(DownloadNotReady):
            await guard.ensure_ready(path)
        assert path.read_bytes() == b"x" * 100
    finally:
        await guard.close()


@pytest.mark.asyncio
async def test_wrong_path_mapping_does_not_match_by_filename(download):
    path, task, file, guard, calls = download
    task["content_path"] = "/downloads-other/movie"
    task["save_path"] = "/downloads-other"
    try:
        await guard.prepare([path])
        assert await guard.reason(path)
    finally:
        await guard.close()


@pytest.mark.asyncio
async def test_connection_failure_blocks_batch(download):
    path, task, file, guard, calls = download
    await guard.close()

    def offline(request):
        raise httpx.ConnectError("offline", request=request)

    guard = DownloadGuard(guard.config, transport=httpx.MockTransport(offline))
    try:
        await guard.prepare([path])
        assert await guard.reason(path)
    finally:
        await guard.close()


@pytest.mark.asyncio
async def test_pipeline_skips_without_scraping_or_failed_move(download, monkeypatch):
    path, task, file, guard, calls = download
    task["state"], task["progress"] = "downloading", 0.01
    config = Config(media_path=str(path.parent.parent), download_guard=guard.config, switch_on=[])
    monkeypatch.setattr(manager, "config", config)
    provider = AsyncMock()
    scraper = Scraper(provider)
    await scraper.download_guard.close()
    scraper.download_guard = guard
    with (
        patch.object(scraper, "process_one_file", new_callable=AsyncMock) as process,
        patch("mdcx.core.scraper.move_file_to_failed_folder", new_callable=AsyncMock) as failed_move,
        patch("mdcx.core.scraper._clean_empty_fodlers", new_callable=AsyncMock) as clean,
    ):
        await scraper.run(FileMode.Default, [path])
        process.assert_not_awaited()
        failed_move.assert_not_awaited()
        clean.assert_not_awaited()
    assert Flags.fail_count == 0
    assert Flags.download_skipped == 1
    assert path.read_bytes() == b"x" * 100
    provider.get.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("unexpected_error", [False, True])
async def test_state_change_never_moves_to_failure_directory(download, monkeypatch, unexpected_error):
    from mdcx.models.types import FileInfo

    path, task, file, guard, calls = download
    monkeypatch.setattr(manager, "config", Config(download_guard=guard.config, switch_on=[]))
    Flags.reset()
    Flags.file_mode = FileMode.Default
    Flags.remain_list = [path]
    scraper = Scraper(AsyncMock())
    await scraper.download_guard.close()
    scraper.download_guard = guard
    await guard.prepare([path])
    info = FileInfo.empty()
    info.file_path, info.folder_path, info.number = path, path.parent, "TEST-123"

    async def changed(*args):
        task["state"] = "checkingDL"
        if unexpected_error:
            raise OSError("source temporarily unavailable")
        raise DownloadNotReady("checkingDL")

    with (
        patch("mdcx.core.scraper.get_file_info_v2", new_callable=AsyncMock, return_value=info),
        patch.object(scraper, "_process_one_file", side_effect=changed),
        patch("mdcx.core.scraper.move_file_to_failed_folder", new_callable=AsyncMock) as move,
    ):
        try:
            await scraper.process_one_file((path, 1, 1))
            move.assert_not_awaited()
            assert Flags.fail_count == 0 and Flags.download_skipped == 1
            assert path.read_bytes() == b"x" * 100
        finally:
            await guard.close()
