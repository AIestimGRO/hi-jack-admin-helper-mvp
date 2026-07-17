from pathlib import Path

from app.db import connect, init_db, transaction
from app.services.import_clients import detect_mapping, import_rows, read_tabular


def test_cp1251_semicolon_import(tmp_path: Path):
    csv_path = tmp_path / "users.csv"
    content = "ID;Telegram ID;Referrer ID;Nickname;Username;First name;Phone\n1;100;9;Иван;ivan;Иван;+7 999 123-45-67\n"
    csv_path.write_bytes(content.encode("cp1251"))
    headers, rows = read_tabular(csv_path)
    mapping = detect_mapping(headers)
    assert mapping["phone_raw"] == "Phone"
    db_path = tmp_path / "db.sqlite3"
    init_db(db_path)
    with transaction(db_path) as conn:
        stats = import_rows(conn, rows, mapping)
    assert stats["inserted"] == 1
    with connect(db_path) as conn:
        client = conn.execute("SELECT * FROM clients").fetchone()
        assert client["phone_local"] == "9991234567"
        assert client["referrer_app_user_id"] == "9"

