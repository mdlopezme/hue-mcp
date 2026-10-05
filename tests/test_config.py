import os
import stat

import pytest

from hue_mcp.config import (
    Location,
    config_path,
    load_config,
    load_location,
    location_path,
    save_config,
    save_location,
)
from hue_mcp.errors import HueError

from conftest import CONFIG


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


def mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_missing_config_means_not_paired():
    assert load_config() is None


def test_round_trip_keeps_the_key_private_and_leaves_nothing_behind():
    save_config(CONFIG)
    assert load_config() == CONFIG
    assert mode(config_path()) == 0o600
    assert mode(config_path().parent) == 0o700
    assert [path.name for path in config_path().parent.iterdir()] == ["bridge.json"]


def test_saving_over_loose_permissions_makes_them_private():
    save_config(CONFIG)
    os.chmod(config_path(), 0o644)
    os.chmod(config_path().parent, 0o775)
    save_config(CONFIG)
    assert mode(config_path()) == 0o600
    assert mode(config_path().parent) == 0o700


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        '{"ip": "192.168.1.50"}',
        '{"bridge_id": "001788fffe123456", "ip": "192.168.1.50", "app_key": ""}',
        '{"bridge_id": "001788fffe123456", "ip": 7, "app_key": "key"}',
    ],
)
def test_unreadable_config_says_how_to_fix_it(content):
    config_path().parent.mkdir(parents=True)
    config_path().write_text(content)
    with pytest.raises(HueError, match="run `hue-mcp setup` again"):
        load_config()


def test_saving_syncs_the_whole_file_to_disk_before_replacing_the_old_one(monkeypatch):
    synced_sizes = []
    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        synced_sizes.append(os.fstat(fd).st_size)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    save_config(CONFIG)
    assert synced_sizes == [config_path().stat().st_size]


def test_a_failed_save_leaves_no_temporary_file(monkeypatch):
    def failing_replace(source, destination):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(OSError, match="disk full"):
        save_config(CONFIG)
    assert list(config_path().parent.iterdir()) == []


def test_a_config_that_cannot_be_read_says_how_to_fix_it():
    config_path().mkdir(parents=True)  # A directory where the file should be.
    with pytest.raises(HueError, match="unreadable"):
        load_config()


def test_a_config_folder_that_cannot_be_entered_is_reported_not_missed():
    save_config(CONFIG)
    config_path().parent.chmod(0o000)
    try:
        if os.access(config_path(), os.R_OK):  # Root can still read it; nothing to test.
            pytest.skip("running with permission to read anything")
        with pytest.raises(HueError, match="unreadable"):
            load_config()
    finally:
        config_path().parent.chmod(0o700)


PLACE = Location(latitude=38.72, longitude=-9.14, label="Lisbon, Portugal")


def test_no_location_until_one_is_set():
    assert load_location() is None


def test_the_location_round_trips_privately():
    save_location(PLACE)
    assert load_location() == PLACE
    assert mode(location_path()) == 0o600


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        '{"latitude": 38.7}',
        '{"latitude": 91, "longitude": 0, "label": "x"}',
        '{"latitude": 0, "longitude": -181, "label": "x"}',
        '{"latitude": "north", "longitude": 0, "label": "x"}',
        '{"latitude": true, "longitude": 0, "label": "x"}',
        '{"latitude": NaN, "longitude": 0, "label": "x"}',
        '{"latitude": 1e400, "longitude": 0, "label": "x"}',
        '{"latitude": 1' + "0" * 400 + ', "longitude": 0, "label": "x"}',  # Overflows a float.
        '{"latitude": 0, "longitude": 0, "label": 7}',
    ],
)
def test_an_unusable_location_says_how_to_fix_it(content):
    location_path().parent.mkdir(parents=True)
    location_path().write_text(content)
    with pytest.raises(HueError, match="set-location"):
        load_location()
