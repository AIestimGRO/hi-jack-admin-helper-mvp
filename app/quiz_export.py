from __future__ import annotations

import json
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.db import connect
from app.product_shell import _require_master
from app.services.quiz import load_builder_questions


EXPORT_FORMAT_VERSION = 1


def _quiz_media_root(settings: Any) -> Path:
    return Path(settings.db_path).resolve().parent / "quiz-media"


def _resolve_quiz_media(
    settings: Any,
    *,
    campaign_code: str,
    web_path: str | None,
) -> tuple[Path, str] | None:
    value = str(web_path or "").strip()
    if not value.startswith("/quiz-media/"):
        return None

    relative = PurePosixPath(value[len("/quiz-media/") :])
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        return None
    if relative.parts[0] != campaign_code:
        return None

    root = _quiz_media_root(settings).resolve()
    target = (root / Path(*relative.parts)).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None
    if not target.is_file():
        return None

    archive_name = f"images/{relative.name}"
    return target, archive_name


def _campaign_snapshot(row: Any) -> dict[str, Any]:
    excluded = {"id", "created_at", "updated_at"}
    return {key: row[key] for key in row.keys() if key not in excluded}


def _stream_file(handle) -> Iterator[bytes]:
    try:
        while True:
            chunk = handle.read(256 * 1024)
            if not chunk:
                break
            yield chunk
    finally:
        handle.close()


def install_quiz_export(app: FastAPI) -> FastAPI:
    if getattr(app.state, "quiz_export_installed", False):
        return app
    app.state.quiz_export_installed = True
    settings = app.state.settings

    @app.get("/api/master/quiz-campaigns/{campaign_id}/export.zip")
    async def export_quiz_zip(request: Request, campaign_id: int):
        _require_master(request)

        with connect(settings.db_path) as conn:
            campaign = conn.execute(
                "SELECT * FROM quiz_campaigns WHERE id=?",
                (campaign_id,),
            ).fetchone()
            if not campaign:
                raise HTTPException(status_code=404, detail="Квиз не найден")

            campaign_code = str(campaign["code"])
            sections = conn.execute(
                """
                SELECT * FROM quiz_sections
                WHERE campaign_code=?
                ORDER BY position,id
                """,
                (campaign_code,),
            ).fetchall()
            questions = load_builder_questions(conn, campaign_code)

        archive = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
        missing_media: list[str] = []
        added_media: set[str] = set()
        section_refs = {int(row["id"]): f"section-{index}" for index, row in enumerate(sections, start=1)}

        def add_media(web_path: str | None) -> str | None:
            value = str(web_path or "").strip()
            if not value:
                return None
            resolved = _resolve_quiz_media(
                settings,
                campaign_code=campaign_code,
                web_path=value,
            )
            if not resolved:
                missing_media.append(value)
                return None
            source, archive_name = resolved
            if archive_name not in added_media:
                zip_file.write(source, archive_name)
                added_media.add(archive_name)
            return archive_name

        with zipfile.ZipFile(
            archive,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as zip_file:
            exported_sections: list[dict[str, Any]] = []
            for row in sections:
                background = str(row["background_image"] or "")
                exported_sections.append(
                    {
                        "ref": section_refs[int(row["id"])],
                        "title": str(row["title"]),
                        "theme": str(row["theme"]),
                        "position": int(row["position"]),
                        "background_image": background or None,
                        "background_image_archive": add_media(background),
                    }
                )

            exported_questions: list[dict[str, Any]] = []
            for question in questions:
                image_path = str(question.get("image_path") or "")
                section_id = question.get("section_id")
                exported_questions.append(
                    {
                        "code": str(question.get("id") or ""),
                        "type": str(question.get("type") or ""),
                        "title": str(question.get("title") or ""),
                        "visual_type": str(question.get("visual_type") or "standard"),
                        "image_path": image_path or None,
                        "image_archive": add_media(image_path),
                        "section_ref": section_refs.get(int(section_id)) if section_id else None,
                        "placeholder": question.get("placeholder"),
                        "accepted_text_answers": list(question.get("accepted_text_answers") or []),
                        "game_round": str(question.get("game_round") or "main"),
                        "required": bool(question.get("required")),
                        "points": int(question.get("points") or 0),
                        "time_limit_seconds": question.get("time_limit_seconds"),
                        "position": int(question.get("position") or 0),
                        "is_active": bool(question.get("is_active")),
                        "options": [
                            {
                                "code": str(option.get("id") or ""),
                                "text": str(option.get("text") or ""),
                                "correct": bool(option.get("correct")),
                                "position": int(option.get("position") or 0),
                                "is_active": bool(option.get("is_active", True)),
                            }
                            for option in question.get("options", [])
                        ],
                    }
                )

            payload = {
                "format": "hi-jack-quiz-export",
                "format_version": EXPORT_FORMAT_VERSION,
                "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "campaign": _campaign_snapshot(campaign),
                "sections": exported_sections,
                "questions": exported_questions,
                "media": {
                    "included": sorted(added_media),
                    "missing": sorted(set(missing_media)),
                },
            }
            zip_file.writestr(
                "quiz.json",
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            )
            zip_file.writestr(
                "README.txt",
                "Hi, Jack! Club quiz export\n"
                f"Campaign: {campaign_code}\n"
                f"Questions: {len(exported_questions)}\n"
                f"Sections: {len(exported_sections)}\n"
                f"Media files: {len(added_media)}\n"
                f"Missing media references: {len(set(missing_media))}\n\n"
                "quiz.json contains the quiz structure and correct answers.\n"
                "images/ contains all available question and section images.\n",
            )

        archive.seek(0)
        version = int(campaign["current_version"] or 1)
        safe_code = "".join(ch for ch in campaign_code if ch.isalnum() or ch in "-_") or "quiz"
        filename = f"quiz-{safe_code}-v{version}.zip"
        return StreamingResponse(
            _stream_file(archive),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return app


__all__ = ["EXPORT_FORMAT_VERSION", "install_quiz_export"]
