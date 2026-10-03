from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from gh_sync import config
from gh_sync.config import (
    ConfigError,
    Settings,
    migrate_legacy_layout,
    parse_interval,
    paths,
    read_env_file,
    update_env_file,
)


class TestHome:
    def test_override_wins(self, isolated_home):
        assert paths().home == isolated_home

    def test_linux_default_follows_xdg(self, monkeypatch, tmp_path):
        monkeypatch.delenv("GH_SYNC_HOME")
        monkeypatch.setattr(config.sys, "platform", "linux")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        assert paths().home == tmp_path / "xdg" / "garmin-hevy-sync"

    def test_macos_default(self, monkeypatch):
        monkeypatch.delenv("GH_SYNC_HOME")
        monkeypatch.setattr(config.sys, "platform", "darwin")
        expected = Path.home() / "Library" / "Application Support" / "garmin-hevy-sync"
        assert paths().home == expected

    def test_windows_default_uses_appdata(self, monkeypatch, tmp_path):
        monkeypatch.delenv("GH_SYNC_HOME")
        monkeypatch.setattr(config.sys, "platform", "win32")
        monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
        assert paths().home == tmp_path / "Roaming" / "garmin-hevy-sync"

    def test_layout(self, isolated_home):
        p = paths()
        assert p.config_env == isolated_home / "config.env"
        assert p.state_db == isolated_home / "state.db"
        assert p.log_file == isolated_home / "logs" / "sync.log"

    def test_token_dir_honours_garmintokens(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GARMINTOKENS", str(tmp_path / "tokens"))
        assert config.garmin_token_dir() == tmp_path / "tokens"


class TestEnvFile:
    def test_reads_quotes_comments_and_export(self, tmp_path):
        path = tmp_path / "x.env"
        path.write_text("# comment\n\nHEVY_API_KEY='abc'\nexport GARMIN_EMAIL=\"me@x.y\"\nBROKEN\n")
        assert read_env_file(path) == {"HEVY_API_KEY": "abc", "GARMIN_EMAIL": "me@x.y"}

    def test_update_keeps_comments_and_order(self, tmp_path):
        path = tmp_path / "x.env"
        path.write_text("# keep me\nHEVY_API_KEY=old\n#GH_LOOKBACK_DAYS=14\nGARMIN_EMAIL=a\n")
        update_env_file(path, {"HEVY_API_KEY": "new", "GH_NOTIFY_URL": "https://n"})
        assert path.read_text().splitlines() == [
            "# keep me",
            "HEVY_API_KEY=new",
            "#GH_LOOKBACK_DAYS=14",
            "GARMIN_EMAIL=a",
            "GH_NOTIFY_URL=https://n",
        ]

    def test_update_with_none_removes_the_key(self, tmp_path):
        path = tmp_path / "x.env"
        path.write_text("A=1\nB=2\n")
        update_env_file(path, {"A": None})
        assert read_env_file(path) == {"B": "2"}

    @pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
    def test_written_owner_only(self, tmp_path):
        path = tmp_path / "x.env"
        update_env_file(path, {"HEVY_API_KEY": "k"})
        assert path.stat().st_mode & 0o777 == 0o600

    def test_environment_beats_the_file(self, isolated_home, monkeypatch):
        isolated_home.mkdir(parents=True, exist_ok=True)
        paths().config_env.write_text("HEVY_API_KEY=from-file\n")
        monkeypatch.setenv("HEVY_API_KEY", "from-env")
        assert Settings.load().hevy_api_key == "from-env"


class TestSettings:
    def test_missing_key_is_a_config_error(self):
        with pytest.raises(ConfigError, match="setup"):
            Settings.load()

    def test_garmin_side_is_optional(self, monkeypatch):
        monkeypatch.setenv("HEVY_API_KEY", "k")
        settings = Settings.load()
        assert settings.garmin_email == ""
        assert settings.import_delay_minutes == 180

    def test_tuning_values_are_parsed(self, monkeypatch):
        monkeypatch.setenv("HEVY_API_KEY", "k")
        monkeypatch.setenv("GH_LOOKBACK_DAYS", "3")
        monkeypatch.setenv("GH_MATCH_THRESHOLD", "0.7")
        monkeypatch.setenv("GH_IMPORT_PRIVATE", "yes")
        settings = Settings.load()
        assert (settings.lookback_days, settings.match_threshold) == (3, 0.7)
        assert settings.import_private is True

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("GH_LOOKBACK_DAYS", "two weeks"),
            ("GH_LOOKBACK_DAYS", "0"),
            ("GH_MATCH_THRESHOLD", "1.5"),
            ("GH_IMPORT_PRIVATE", "maybe"),
        ],
    )
    def test_bad_values_name_the_variable(self, monkeypatch, name, value):
        monkeypatch.setenv("HEVY_API_KEY", "k")
        monkeypatch.setenv(name, value)
        with pytest.raises(ConfigError, match=name):
            Settings.load()


class TestParseInterval:
    @pytest.mark.parametrize(
        ("text", "minutes"),
        [("30", 30), ("30m", 30), ("45 min", 45), ("1h", 60), ("2 hours", 120), ("12H", 720)],
    )
    def test_accepted_forms(self, text, minutes):
        assert parse_interval(text) == minutes

    @pytest.mark.parametrize("text", ["", "soon", "1d", "-5m", "0m"])
    def test_rejected_forms(self, text):
        with pytest.raises(ConfigError):
            parse_interval(text)


class TestMigrateLegacyLayout:
    @staticmethod
    def _checkout(root: Path) -> Path:
        (root / "config").mkdir(parents=True)
        (root / "data").mkdir()
        (root / ".env").write_text(
            "HEVY_API_KEY=key\nGARMIN_EMAIL=me@example.com\nGARMIN_PASSWORD=hunter2\n"
            "GH_LOOKBACK_DAYS=21\n"
        )
        (root / "config" / "profile.json").write_text('{"merge_watch_strategy": "merge"}')
        conn = sqlite3.connect(root / "data" / "state.db")
        conn.execute("CREATE TABLE marker (x TEXT)")
        conn.commit()
        conn.close()
        (root / "data" / "exercise_map.json").write_text("{}")
        return root

    @pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
    def test_new_files_are_private(self, tmp_path):
        root = self._checkout(tmp_path / "checkout")
        migrate_legacy_layout(paths(), checkout_root=root)
        assert paths().home.stat().st_mode & 0o777 == 0o700
        assert paths().config_env.stat().st_mode & 0o777 == 0o600

    def test_password_is_removed_from_the_old_env_and_hevy2garmin(self, tmp_path):
        import json

        root = self._checkout(tmp_path / "checkout")
        stored = config.hevy2garmin_home() / "config.json"
        stored.parent.mkdir(parents=True)
        stored.write_text(json.dumps({"garmin_password": "hunter2", "hevy_api_key": "key"}))
        notes = migrate_legacy_layout(paths(), checkout_root=root)
        assert "hunter2" not in (root / ".env").read_text()
        assert read_env_file(root / ".env")["HEVY_API_KEY"] == "key"
        assert json.loads(stored.read_text()) == {"hevy_api_key": "key"}
        assert any(str(stored) in note for note in notes)

    def test_copies_everything_but_the_password(self, tmp_path):
        root = self._checkout(tmp_path / "checkout")
        notes = migrate_legacy_layout(paths(), checkout_root=root)
        env = read_env_file(paths().config_env)
        assert env["HEVY_API_KEY"] == "key"
        assert env["GH_LOOKBACK_DAYS"] == "21"
        assert "GARMIN_PASSWORD" not in env
        assert "hunter2" not in paths().config_env.read_text()
        assert paths().profile.exists() and paths().exercise_map.exists()
        assert sqlite3.connect(paths().state_db).execute("SELECT * FROM marker").fetchall() == []
        assert any("GARMIN_PASSWORD" in note for note in notes)
        # The originals stay behind as a backup (minus the password).
        assert (root / ".env").exists() and (root / "data" / "state.db").exists()

    def test_runs_only_once(self, tmp_path):
        root = self._checkout(tmp_path / "checkout")
        assert migrate_legacy_layout(paths(), checkout_root=root)
        assert migrate_legacy_layout(paths(), checkout_root=root) == []

    def test_no_checkout_means_nothing_to_do(self, tmp_path):
        assert migrate_legacy_layout(paths(), checkout_root=tmp_path / "nothing") == []
        assert not paths().config_env.exists()
