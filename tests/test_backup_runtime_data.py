from pathlib import Path

from scripts.backup import copy_runtime_data


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def test_runtime_backup_keeps_media_and_skips_live_sqlite_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    destination = tmp_path / "backup"

    _write(project / "data" / "quiz-media" / "question.png", "quiz")
    _write(project / "data" / "reward-media" / "reward.png", "reward")
    _write(project / "data" / "uploads" / "avatar.png", "avatar")
    _write(project / "data" / "quiz_questions.json", "{}")
    _write(project / "data" / "club_tools.sqlite3", "live")
    _write(project / "data" / "club_tools.sqlite3-wal", "wal")
    _write(project / "data" / "club_tools.sqlite3.bak-older", "old")

    copy_runtime_data(project, destination)

    assert (destination / "data" / "quiz-media" / "question.png").read_text() == "quiz"
    assert (destination / "data" / "reward-media" / "reward.png").read_text() == "reward"
    assert (destination / "data" / "uploads" / "avatar.png").read_text() == "avatar"
    assert (destination / "data" / "quiz_questions.json").read_text() == "{}"
    assert not (destination / "data" / "club_tools.sqlite3").exists()
    assert not (destination / "data" / "club_tools.sqlite3-wal").exists()
    assert not (destination / "data" / "club_tools.sqlite3.bak-older").exists()
