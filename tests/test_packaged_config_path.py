from mdcx.consts import packaged_main_path
from mdcx.crawlers.dmm_new import DmmCrawler


def test_packaged_config_is_relative_to_executable_not_working_directory(tmp_path, monkeypatch):
    exe = tmp_path / "installed" / "MDCx.exe"
    exe.parent.mkdir()
    elsewhere = tmp_path / "different-working-directory"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert packaged_main_path(str(exe), False) == exe.parent


def test_fanza_tmavr_vendor_prefix():
    assert DmmCrawler.video_cids("TMAVR-287") == ["55tmavr00287"]
    assert DmmCrawler.video_cids("MDVR-438") == ["mdvr00438"]
