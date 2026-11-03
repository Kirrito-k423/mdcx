from pathlib import Path
from unittest.mock import patch

import pytest
from PyQt5.QtWidgets import QApplication, QMessageBox

from mdcx.base.file import save_remain_list
from mdcx.config.enums import Switch
from mdcx.core import scraper
from mdcx.models.flags import Flags


@pytest.fixture(scope="session")
def qt_app():
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
        yield QApplication.instance() or QApplication([])


@pytest.fixture
def resume_file(tmp_path, monkeypatch, qt_app):
    remain = tmp_path / "remain.txt"
    monkeypatch.setattr(scraper.resources, "u", lambda _: remain)
    monkeypatch.setattr(scraper.manager.config, "media_path", str(tmp_path))
    monkeypatch.setattr(scraper.manager.config, "scrape_softlink_path", False)
    monkeypatch.setattr(scraper.manager.config, "switch_on", [Switch.REMAIN_TASK])
    return remain


@pytest.mark.parametrize("restart", [False, True])
def test_corrupt_resume_list_requires_explicit_restart(resume_file, restart):
    first = resume_file.parent / "first.mp4"
    second = resume_file.parent / "second.mp4"
    resume_file.write_text(f"......{first}{second}", encoding="utf-8")
    response = QMessageBox.Yes if restart else QMessageBox.Cancel
    with (
        patch.object(QMessageBox, "exec", side_effect=[QMessageBox.Yes, response]) as message_box,
        patch.object(scraper, "start_new_scrape") as start,
    ):
        assert scraper.get_remain_list() is not restart
        assert message_box.call_count == 2
        start.assert_not_called()


def test_resume_round_trip_saves_pending_files_only(resume_file, monkeypatch):
    pending = [resume_file.parent / "first.mp4", resume_file.parent / "second.mp4"]
    monkeypatch.setattr(Flags, "can_save_remain", True)
    monkeypatch.setattr(Flags, "remain_list", pending)
    monkeypatch.setattr(Flags, "success_list", {Path("completed.mp4")})
    save_remain_list()

    with (
        patch.object(QMessageBox, "exec", return_value=QMessageBox.Yes),
        patch.object(scraper, "start_new_scrape") as start,
    ):
        assert scraper.get_remain_list()
        start.assert_called_once_with(scraper.FileMode.Default, pending)
