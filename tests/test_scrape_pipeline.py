import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from mdcx.config.enums import CDChar, Website
from mdcx.config.manager import manager
from mdcx.config.models import Config
from mdcx.core.file import deal_old_files, get_file_info_v2
from mdcx.core.file_crawler import FileScraper
from mdcx.core.scraper import Scraper
from mdcx.crawlers.dmm_new import DmmCrawler
from mdcx.models.enums import FileMode
from mdcx.models.flags import Flags
from mdcx.models.types import CrawlerDebugInfo, CrawlerInput, CrawlerResponse, FileInfo, OtherInfo
from mdcx.number import get_file_number


@pytest.fixture
def isolated_config(monkeypatch):
    monkeypatch.setattr(manager, "config", Config())
    Flags.reset()
    return manager.config


@pytest.mark.parametrize(
    "name,expected",
    [
        ("13dsvr01997_8k (1).mp4", "3DSVR-1997"),
        ("3DSVR-1997-cd2-8K.mp4", "3DSVR-1997"),
        ("kiwvr00914_8k (2).mp4", "KIWVR-914"),
        ("TMAVR-287-cd3-8K.mp4", "TMAVR-287"),
    ],
)
def test_vr_numbers(name, expected):
    assert get_file_number(name, []) == expected


@pytest.mark.asyncio
async def test_failure_releases_next_part(isolated_config, monkeypatch):
    scraper = Scraper(AsyncMock())
    scraper._process_one_file_unlocked = AsyncMock(side_effect=[ValueError("failure"), (None, None)])
    info = FileInfo.empty()
    info.number = "TEST-123"
    first, second = await asyncio.wait_for(
        asyncio.gather(
            scraper._process_one_file(info, FileMode.Default),
            scraper._process_one_file(info, FileMode.Default),
            return_exceptions=True,
        ),
        timeout=1,
    )
    assert isinstance(first, ValueError)
    assert second == (None, None)


@pytest.mark.asyncio
async def test_parts_reuse_failed_metadata_without_mutating_input(isolated_config):
    crawler = AsyncMock()
    crawler.run.return_value = CrawlerResponse(debug_info=CrawlerDebugInfo())
    provider = AsyncMock()
    provider.get.return_value = crawler
    scraper = FileScraper(isolated_config, provider)
    task = CrawlerInput.empty()
    task.number, task.short_number = "259LUXU-1234", "LUXU-1234"
    await asyncio.gather(*(scraper._call_crawler(task, Website.DMM) for _ in range(3)))
    assert crawler.run.await_count == 1
    assert task.number == "259LUXU-1234"


@pytest.mark.asyncio
async def test_vr_letter_parts_before_resolution(tmp_path, isolated_config):
    isolated_config.cd_char = list(CDChar)
    first = await get_file_info_v2(tmp_path / "SAVR-1148_A_4096p_8K_LR_180.mp4", copy_sub=False)
    second = await get_file_info_v2(tmp_path / "SAVR-1148_B_4096p_8K_LR_180.mp4", copy_sub=False)
    assert first.cd_part.lower() == "-cd1"
    assert second.cd_part.lower() == "-cd2"


@pytest.mark.asyncio
async def test_missing_images_are_not_old_images(tmp_path, isolated_config):
    Flags.file_done_dic["TEST-123"] = dict.fromkeys(
        ["poster", "thumb", "fanart", "trailer", "local_poster", "local_thumb", "local_fanart", "local_trailer"]
    )
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    info = OtherInfo.empty()
    await deal_old_files(
        "TEST-123",
        info,
        old,
        new,
        old / "movie.mp4",
        new / "movie.mp4",
        new / "movie-thumb.jpg",
        new / "movie-poster.jpg",
        new / "movie-fanart.jpg",
        new / "movie.nfo",
        ".mp4",
        new / "poster.jpg",
        new / "thumb.jpg",
        new / "fanart.jpg",
    )
    assert info.thumb_path is None
    assert info.poster_path is None
    assert info.fanart_path is None


@pytest.mark.asyncio
async def test_video_api_skips_browser_and_returns_images():
    client = AsyncMock()
    client.post_json.return_value = (
        {
            "data": {
                "ppvContent": {
                    "id": "13dsvr01997",
                    "title": "Test title",
                    "description": "Test plot",
                    "packageImage": {
                        "largeUrl": "https://example.com/cover.jpg",
                        "mediumUrl": "https://example.com/poster.jpg",
                    },
                    "sampleImages": [{"largeImageUrl": "https://example.com/sample.jpg"}],
                }
            }
        },
        "",
    )
    crawler = DmmCrawler(client)
    crawler.browser_factory = AsyncMock()
    task = CrawlerInput.empty()
    task.number = "3DSVR-1997"
    result = await crawler.run(task)
    assert result.data.title == "Test title"
    assert result.data.thumb == "https://example.com/cover.jpg"
    assert result.data.extrafanart == ["https://example.com/sample.jpg"]
    crawler.browser_factory.assert_not_awaited()


@pytest.mark.asyncio
async def test_denied_rename_never_copies_or_deletes_target(tmp_path, monkeypatch):
    from mdcx.utils import file as file_utils

    old, new = tmp_path / "old.mp4", tmp_path / "new.mp4"
    old.write_bytes(b"source")
    new.write_bytes(b"existing target")
    rename = Mock(side_effect=PermissionError(13, "access denied"))
    copy_move = Mock()
    monkeypatch.setattr(file_utils.os, "replace", rename)
    monkeypatch.setattr(file_utils.shutil, "move", copy_move)
    success, error = await file_utils.move_file_async(old, new)
    assert not success and "access denied" in error
    copy_move.assert_not_called()
    assert old.read_bytes() == b"source"
    assert new.read_bytes() == b"existing target"
