from __future__ import annotations

import inspect
import json
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.requests import Request as StarletteRequest

from app.db import transaction
from app.services.quiz import attempt_token_hash


ANTI_CHEAT_DEBOUNCE_MS = 1_000
ANTI_CHEAT_SKIP_SENTINEL = "__hj_visibility_skip__"
ANTI_CHEAT_ASSET = "/static/js/jackside-anticheat.js"


def _skip_value(question: dict[str, Any]) -> Any:
    if str(question.get("type") or "") == "multi_choice":
        return [ANTI_CHEAT_SKIP_SENTINEL]
    return ANTI_CHEAT_SKIP_SENTINEL


def _is_skip_value(question: dict[str, Any], value: Any) -> bool:
    if str(question.get("type") or "") == "multi_choice":
        return value == [ANTI_CHEAT_SKIP_SENTINEL]
    return value == ANTI_CHEAT_SKIP_SENTINEL


def apply_visibility_skip(
    conn,
    *,
    token_hash: str,
    question_id: str,
    hidden_ms: int,
) -> dict[str, Any]:
    if hidden_ms < ANTI_CHEAT_DEBOUNCE_MS:
        return {
            "skipped": False,
            "reason": "debounce_not_reached",
            "debounce_ms": ANTI_CHEAT_DEBOUNCE_MS,
        }

    attempt = conn.execute(
        "SELECT * FROM quiz_attempts WHERE token_hash=?",
        (token_hash,),
    ).fetchone()
    if not attempt:
        raise ValueError("attempt_not_found")
    if str(attempt["status"] or "") != "in_progress":
        return {
            "skipped": False,
            "reason": "attempt_finished",
            "finished": True,
        }

    campaign = conn.execute(
        "SELECT campaign_type FROM quiz_campaigns WHERE code=?",
        (attempt["campaign_code"],),
    ).fetchone()
    if not campaign or str(campaign["campaign_type"] or "") != "daily_414":
        raise ValueError("anti_cheat_not_enabled")

    try:
        questions = json.loads(str(attempt["questions_snapshot_json"] or "[]"))
        answers = json.loads(str(attempt["answers_json"] or "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError("attempt_snapshot_invalid") from exc
    if not isinstance(questions, list) or not isinstance(answers, dict) or not questions:
        raise ValueError("attempt_snapshot_invalid")

    normalized_question_id = str(question_id or "")
    question_index = next(
        (
            index
            for index, item in enumerate(questions)
            if isinstance(item, dict) and str(item.get("id") or "") == normalized_question_id
        ),
        None,
    )
    if question_index is None:
        raise ValueError("question_not_found")
    question = questions[question_index]
    current_index = int(attempt["current_index"] or 0)
    last_question = question_index == len(questions) - 1

    if normalized_question_id in answers:
        if _is_skip_value(question, answers[normalized_question_id]):
            return {
                "skipped": True,
                "already_applied": True,
                "reason": "already_skipped",
                "question_id": normalized_question_id,
                "question_index": question_index,
                "current_index": current_index,
                "last_question": last_question,
                "finish_required": last_question,
            }
        return {
            "skipped": False,
            "reason": "already_answered",
            "question_id": normalized_question_id,
            "question_index": question_index,
            "current_index": current_index,
            "last_question": last_question,
        }

    if question_index < current_index:
        return {
            "skipped": False,
            "reason": "already_advanced",
            "question_id": normalized_question_id,
            "question_index": question_index,
            "current_index": current_index,
            "last_question": last_question,
        }
    if question_index != current_index:
        raise ValueError("question_not_current")

    answers[normalized_question_id] = _skip_value(question)
    next_index = min(question_index + 1, len(questions) - 1)
    conn.execute(
        """
        UPDATE quiz_attempts
        SET answers_json=?, current_index=?, last_activity_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (
            json.dumps(answers, ensure_ascii=False, sort_keys=True),
            next_index,
            int(attempt["id"]),
        ),
    )
    return {
        "skipped": True,
        "already_applied": False,
        "reason": "visibility_timeout",
        "question_id": normalized_question_id,
        "question_index": question_index,
        "current_index": next_index,
        "last_question": last_question,
        "finish_required": last_question,
        "debounce_ms": ANTI_CHEAT_DEBOUNCE_MS,
    }


def _route_endpoint(app: FastAPI, path: str) -> Callable[..., Any]:
    for route in app.routes:
        if (
            isinstance(route, APIRoute)
            and route.path == path
            and "POST" in (route.methods or set())
        ):
            return route.endpoint
    raise RuntimeError(f"route_not_found:{path}")


def _json_request(request: Request, *, path: str, payload: dict[str, Any]) -> StarletteRequest:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    scope = dict(request.scope)
    scope["method"] = "POST"
    scope["path"] = path
    scope["raw_path"] = path.encode("ascii")
    headers = [
        (key, value)
        for key, value in request.scope.get("headers", [])
        if key.lower() not in {b"content-type", b"content-length"}
    ]
    headers.extend(
        [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
    )
    scope["headers"] = headers
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return StarletteRequest(scope, receive)


def install_jackside_anticheat(app: FastAPI) -> FastAPI:
    if getattr(app.state, "jackside_anticheat_installed", False):
        return app
    app.state.jackside_anticheat_installed = True
    settings = app.state.settings
    finish_endpoint = _route_endpoint(app, "/api/quiz/finish")

    @app.middleware("http")
    async def jackside_anticheat_asset_middleware(request: Request, call_next):
        response = await call_next(request)
        if request.url.path != "/quiz":
            return response
        content_type = str(response.headers.get("content-type") or "")
        if "text/html" not in content_type or not hasattr(response, "body_iterator"):
            return response
        chunks: list[bytes] = []
        async for chunk in response.body_iterator:
            chunks.append(chunk if isinstance(chunk, bytes) else bytes(chunk))
        body = b"".join(chunks)
        marker = ANTI_CHEAT_ASSET.encode("utf-8")
        if marker not in body and b"</body>" in body:
            body = body.replace(
                b"</body>",
                f'<script data-jackside-anticheat src="{ANTI_CHEAT_ASSET}" defer></script></body>'.encode(
                    "utf-8"
                ),
                1,
            )

        async def body_iterator():
            yield body

        response.body_iterator = body_iterator()
        response.headers["content-length"] = str(len(body))
        return response

    @app.post("/api/quiz/anti-cheat/skip", response_class=JSONResponse)
    async def anti_cheat_skip(request: Request):
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="Некорректные данные") from exc

        token = str(payload.get("attempt_token") or "")
        question_id = str(payload.get("question_id") or "")
        try:
            hidden_ms = int(payload.get("hidden_ms") or 0)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="Некорректное время ухода") from exc
        if len(token) < 32 or not question_id:
            raise HTTPException(status_code=404, detail="Попытка не найдена")

        token_hash = attempt_token_hash(settings.secret_key, token)
        try:
            with transaction(settings.db_path) as conn:
                result = apply_visibility_skip(
                    conn,
                    token_hash=token_hash,
                    question_id=question_id,
                    hidden_ms=hidden_ms,
                )
        except ValueError as exc:
            messages = {
                "attempt_not_found": "Попытка не найдена",
                "anti_cheat_not_enabled": "Защита ухода со страницы для этого квиза не включена",
                "attempt_snapshot_invalid": "Данные попытки повреждены",
                "question_not_found": "Вопрос не найден в этой попытке",
                "question_not_current": "Этот вопрос уже не является текущим",
            }
            code = str(exc)
            raise HTTPException(
                status_code=404 if code == "attempt_not_found" else 409,
                detail=messages.get(code, code),
            ) from exc

        if result.get("finish_required"):
            finish_request = _json_request(
                request,
                path="/api/quiz/finish",
                payload={"attempt_token": token},
            )
            finish_result = finish_endpoint(finish_request)
            if inspect.isawaitable(finish_result):
                finish_result = await finish_result
            if isinstance(finish_result, JSONResponse):
                finish_result = json.loads(finish_result.body.decode("utf-8"))
            result["finished"] = True
            result["result"] = finish_result

        return JSONResponse(result, headers={"Cache-Control": "private, no-store"})

    return app


__all__ = [
    "ANTI_CHEAT_DEBOUNCE_MS",
    "ANTI_CHEAT_SKIP_SENTINEL",
    "apply_visibility_skip",
    "install_jackside_anticheat",
]
