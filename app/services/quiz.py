from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from app.services.clients import clean
from app.services.phone import full_phone, normalize_phone


CAMPAIGN_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,49}$")
QUESTION_TYPES = {"single_choice", "multi_choice", "text"}


def normalize_campaign(value: Any) -> str:
    campaign = str(value or "default").strip().lower()
    return campaign if CAMPAIGN_RE.fullmatch(campaign) else "default"


def parse_quick_questions(value: str) -> list[dict[str, Any]]:
    """Parse question blocks where * marks correct and - marks wrong answers."""
    text = str(value or "").strip()
    if not text or len(text) > 50_000:
        raise ValueError("Вставьте вопросы (не более 50 000 символов)")
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    if not blocks or len(blocks) > 100:
        raise ValueError("Можно добавить от 1 до 100 вопросов за один раз")
    questions: list[dict[str, Any]] = []
    for number, block in enumerate(blocks, start=1):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 3:
            raise ValueError(f"Блок {number}: нужен вопрос и минимум два варианта")
        title = lines[0]
        if title.startswith(("*", "-")) or len(title) < 2 or len(title) > 300:
            raise ValueError(f"Блок {number}: проверьте текст вопроса")
        options: list[dict[str, Any]] = []
        for line in lines[1:]:
            if not line.startswith(("*", "-")):
                raise ValueError(f"Блок {number}: каждый вариант должен начинаться с * или -")
            option_text = line[1:].strip()
            if not option_text or len(option_text) > 300:
                raise ValueError(f"Блок {number}: проверьте текст варианта")
            options.append({"text": option_text, "is_correct": line.startswith("*")})
        if len(options) < 2:
            raise ValueError(f"Блок {number}: добавьте минимум два варианта")
        if len({option["text"].casefold() for option in options}) != len(options):
            raise ValueError(f"Блок {number}: варианты ответов не должны повторяться")
        correct_count = sum(int(option["is_correct"]) for option in options)
        if correct_count < 1:
            raise ValueError(f"Блок {number}: отметьте правильный ответ звёздочкой")
        questions.append({
            "title": title,
            "question_type": "multi_choice" if correct_count > 1 else "single_choice",
            "options": options,
        })
    return questions


def load_questions(path: str | Path, campaign: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("questions_unavailable") from exc
    if not isinstance(payload, list):
        raise ValueError("questions_invalid")
    normalized_campaign = normalize_campaign(campaign)
    selected = [item for item in payload if isinstance(item, dict) and item.get("campaign") == normalized_campaign]
    if not selected and normalized_campaign != "default":
        selected = [item for item in payload if isinstance(item, dict) and item.get("campaign") == "default"]
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in selected:
        question_id = str(item.get("id", ""))
        kind = item.get("type")
        title = str(item.get("title", "")).strip()
        if not question_id or question_id in seen or kind not in QUESTION_TYPES or not title:
            raise ValueError("questions_invalid")
        seen.add(question_id)
        result.append(item)
    if not result:
        raise ValueError("questions_unavailable")
    return result


def seed_questions_from_json(conn: sqlite3.Connection, path: str | Path) -> int:
    if conn.execute("SELECT 1 FROM quiz_questions LIMIT 1").fetchone():
        return 0
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("questions_unavailable") from exc
    if not isinstance(payload, list):
        raise ValueError("questions_invalid")
    campaign_positions: dict[str, int] = {}
    inserted = 0
    for item in payload:
        if not isinstance(item, dict):
            continue
        campaign = normalize_campaign(item.get("campaign"))
        code = str(item.get("id", "")).strip()
        kind = item.get("type")
        title = str(item.get("title", "")).strip()
        if not code or kind not in QUESTION_TYPES or not title:
            raise ValueError("questions_invalid")
        campaign_positions[campaign] = campaign_positions.get(campaign, 0) + 10
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO quiz_questions(
                campaign_code, code, type, title, placeholder, required, points, position, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, 1)
            """,
            (
                campaign, code, kind, title, str(item.get("placeholder", "")).strip() or None,
                int(bool(item.get("required"))), campaign_positions[campaign],
            ),
        )
        if not cursor.rowcount:
            continue
        question_id = int(cursor.lastrowid)
        for index, option in enumerate(item.get("options", []), start=1):
            if not isinstance(option, dict):
                continue
            option_code = str(option.get("id", "")).strip()
            option_text = str(option.get("text", "")).strip()
            if option_code and option_text:
                conn.execute(
                    """
                    INSERT INTO quiz_options(question_id, code, text, is_correct, position)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (question_id, option_code, option_text, int(bool(option.get("correct"))), index * 10),
                )
        inserted += 1
    return inserted


def _db_questions(
    conn: sqlite3.Connection,
    campaign: str,
    *,
    active_only: bool,
    fallback: bool,
) -> list[dict[str, Any]]:
    campaign = normalize_campaign(campaign)
    active_clause = "AND is_active = 1" if active_only else ""
    rows = conn.execute(
        f"""
        SELECT qq.*, qs.title AS section_title, qs.theme AS section_theme,
               qs.background_image AS section_background_image, qs.position AS section_position
        FROM quiz_questions qq
        LEFT JOIN quiz_sections qs ON qs.id=qq.section_id
        WHERE qq.campaign_code = ? {active_clause}
        ORDER BY COALESCE(qs.position, 0), qq.position, qq.id
        """,
        (campaign,),
    ).fetchall()
    if not rows and fallback and campaign != "default":
        rows = conn.execute(
            f"""
            SELECT qq.*, qs.title AS section_title, qs.theme AS section_theme,
                   qs.background_image AS section_background_image, qs.position AS section_position
            FROM quiz_questions qq
            LEFT JOIN quiz_sections qs ON qs.id=qq.section_id
            WHERE qq.campaign_code = 'default' {active_clause}
            ORDER BY COALESCE(qs.position, 0), qq.position, qq.id
            """
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        options = conn.execute(
            f"SELECT * FROM quiz_options WHERE question_id = ? {'AND is_active = 1' if active_only else ''} ORDER BY position, id",
            (row["id"],),
        ).fetchall()
        result.append({
            "db_id": row["id"],
            "id": row["code"],
            "campaign": row["campaign_code"],
            "type": row["type"],
            "title": row["title"],
            "visual_type": row["visual_type"] or "standard",
            "image_path": row["image_path"],
            "section_id": row["section_id"],
            "section": {
                "title": row["section_title"] or "",
                "theme": row["section_theme"] or "theory",
                "background_image": row["section_background_image"],
            },
            "placeholder": row["placeholder"],
            "required": bool(row["required"]),
            "points": int(row["points"]),
            "time_limit_seconds": row["time_limit_seconds"],
            "position": int(row["position"]),
            "is_active": bool(row["is_active"]),
            "options": [
                {
                    "db_id": option["id"],
                    "id": option["code"],
                    "text": option["text"],
                    "correct": bool(option["is_correct"]),
                    "position": int(option["position"]),
                    "is_active": bool(option["is_active"]),
                }
                for option in options
            ],
        })
    return result


def load_db_questions(conn: sqlite3.Connection, campaign: str) -> list[dict[str, Any]]:
    questions = _db_questions(conn, campaign, active_only=True, fallback=True)
    if not questions:
        raise ValueError("questions_unavailable")
    for question in questions:
        if question["type"] in {"single_choice", "multi_choice"} and len(question["options"]) < 2:
            raise ValueError("questions_incomplete")
    return questions


def load_builder_questions(conn: sqlite3.Connection, campaign: str) -> list[dict[str, Any]]:
    return _db_questions(conn, campaign, active_only=False, fallback=False)


def validate_answers(questions: list[dict[str, Any]], answers: Any) -> dict[str, Any]:
    if not isinstance(answers, dict):
        raise ValueError("answers_invalid")
    validated: dict[str, Any] = {}
    for question in questions:
        question_id = question["id"]
        kind = question["type"]
        value = answers.get(question_id)
        required = bool(question.get("required"))
        option_ids = {str(option.get("id")) for option in question.get("options", []) if isinstance(option, dict)}
        if kind == "single_choice":
            value = str(value or "")
            if value and value not in option_ids:
                raise ValueError("answers_invalid")
            if required and not value:
                raise ValueError("answers_required")
        elif kind == "multi_choice":
            if value is None:
                value = []
            if not isinstance(value, list) or len(value) > len(option_ids):
                raise ValueError("answers_invalid")
            value = list(dict.fromkeys(str(item) for item in value))
            if any(item not in option_ids for item in value):
                raise ValueError("answers_invalid")
            if required and not value:
                raise ValueError("answers_required")
        else:
            value = str(value or "").strip()
            if len(value) > 1000:
                raise ValueError("answer_too_long")
            if required and not value:
                raise ValueError("answers_required")
        validated[question_id] = value
    return validated


def score_answers(questions: list[dict[str, Any]], answers: dict[str, Any]) -> dict[str, int]:
    score = 0
    max_score = 0
    correct_count = 0
    max_correct_count = 0
    for question in questions:
        if question["type"] == "text":
            continue
        correct = {str(option["id"]) for option in question.get("options", []) if option.get("correct")}
        if not correct:
            continue
        max_correct_count += 1
        points = max(0, int(question.get("points", 1)))
        max_score += points
        value = answers.get(question["id"])
        selected = set(str(item) for item in value) if isinstance(value, list) else ({str(value)} if value else set())
        if selected == correct:
            correct_count += 1
            score += points
    return {
        "score": score,
        "max_score": max_score,
        "correct_count": correct_count,
        "max_correct_count": max_correct_count,
    }


def upsert_quiz_client(
    conn: sqlite3.Connection,
    *,
    phone_raw: str,
    name: str | None,
    username: str | None,
    nickname: str | None,
) -> tuple[int, bool]:
    phone_local = normalize_phone(phone_raw)
    if not phone_local:
        raise ValueError("phone_invalid")
    existing = conn.execute("SELECT id FROM clients WHERE phone_local = ?", (phone_local,)).fetchone()
    values = {
        "first_name": clean(name),
        "username": clean(username.lstrip("@") if username else None),
        "nickname": clean(nickname),
    }
    if existing:
        conn.execute(
            """
            UPDATE clients SET
                first_name = CASE WHEN first_name IS NULL OR first_name = '' THEN ? ELSE first_name END,
                username = CASE WHEN username IS NULL OR username = '' THEN ? ELSE username END,
                nickname = CASE WHEN nickname IS NULL OR nickname = '' THEN ? ELSE nickname END,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (values["first_name"], values["username"], values["nickname"], existing["id"]),
        )
        return int(existing["id"]), False
    cursor = conn.execute(
        """
        INSERT INTO clients(first_name, username, nickname, phone_raw, phone_full, phone_local, source)
        VALUES (?, ?, ?, ?, ?, ?, 'quiz')
        """,
        (values["first_name"], values["username"], values["nickname"], phone_raw, full_phone(phone_local), phone_local),
    )
    return int(cursor.lastrowid), True


def ip_fingerprint(secret_key: str, ip_address: str) -> str:
    return hmac.new(secret_key.encode("utf-8"), ip_address.encode("utf-8"), hashlib.sha256).hexdigest()


def attempt_token_hash(secret_key: str, token: str) -> str:
    return hmac.new(
        secret_key.encode("utf-8"), f"quiz-attempt:{token}".encode("utf-8"), hashlib.sha256
    ).hexdigest()


def public_questions(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    allowed = {
        "id", "type", "title", "options", "required", "placeholder",
        "visual_type", "image_path", "section",
    }
    result = []
    for question in questions:
        public = {key: value for key, value in question.items() if key in allowed and key != "options"}
        public["options"] = [
            {"id": option["id"], "text": option["text"]}
            for option in question.get("options", [])
        ]
        result.append(public)
    return result


def answer_summary(questions: list[dict[str, Any]], answers_json: str) -> list[tuple[str, str, bool | None]]:
    try:
        answers = json.loads(answers_json)
    except (TypeError, json.JSONDecodeError):
        return [("Ответы", "Не удалось прочитать", None)]
    result: list[tuple[str, str, bool | None]] = []
    for question in questions:
        value = answers.get(question["id"])
        options = {
            str(option.get("id")): str(option.get("text", option.get("id")))
            for option in question.get("options", []) if isinstance(option, dict)
        }
        if isinstance(value, list):
            display = ", ".join(options.get(str(item), str(item)) for item in value)
            selected = {str(item) for item in value}
        else:
            display = options.get(str(value), str(value or "—"))
            selected = {str(value)} if value else set()
        correct_options = {
            str(option.get("id")) for option in question.get("options", []) if option.get("correct")
        }
        is_correct = selected == correct_options if correct_options else None
        result.append((question["title"], display, is_correct))
    return result
