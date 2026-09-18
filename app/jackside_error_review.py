from __future__ import annotations

import inspect
import json
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.routing import APIRoute

from app.db import connect, transaction
from app.product_shell import _check_csrf, _current_member, _require_master
from app.services.member_accounts import jackcoin_balance
from app.services.quiz import normalize_campaign, normalize_text_answer


DEFAULT_ERROR_REVIEW_PRICE_JC = 30
MAX_ERROR_REVIEW_PRICE_JC = 100_000
MAX_EXPLANATION_LENGTH = 4_000
ADMIN_ASSET = "/static/js/jackside-error-review-admin.js"
MEMBER_ASSET = "/static/js/jackside-error-review.js"


def _safe_json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_json_list(value: Any) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, json.JSONDecodeError):
        return []
    return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []


def _question_is_wrong(question: dict[str, Any], answer: Any) -> bool:
    kind = str(question.get("type") or "")
    if kind == "text":
        accepted = {
            normalize_text_answer(item)
            for item in question.get("accepted_text_answers", [])
            if normalize_text_answer(item)
        }
        if not accepted:
            return False
        return normalize_text_answer(answer) not in accepted

    correct = {
        str(option.get("id"))
        for option in question.get("options", [])
        if isinstance(option, dict) and option.get("correct")
    }
    if not correct:
        return False
    if isinstance(answer, list):
        selected = {str(item) for item in answer}
    else:
        selected = {str(answer)} if answer not in (None, "") else set()
    return selected != correct


def _review_payload(
    snapshot: sqlite3.Row,
    explanations: dict[str, str],
) -> list[dict[str, Any]]:
    questions = _safe_json_list(snapshot["questions_snapshot_json"])
    answers = _safe_json_object(snapshot["answers_json"])
    result: list[dict[str, Any]] = []
    for question in questions:
        question_code = str(question.get("id") or "")
        if not question_code:
            continue
        answer = answers.get(question_code)
        if not _question_is_wrong(question, answer):
            continue
        kind = str(question.get("type") or "single_choice")
        item: dict[str, Any] = {
            "id": question_code,
            "title": str(question.get("title") or ""),
            "type": kind,
            "visual_type": str(question.get("visual_type") or "standard"),
            "image_path": question.get("image_path"),
            "explanation": explanations.get(question_code, ""),
        }
        if kind == "text":
            item["user_answer"] = str(answer or "")
            item["correct_answers"] = [
                str(value)
                for value in question.get("accepted_text_answers", [])
                if str(value).strip()
            ]
        else:
            selected = (
                {str(value) for value in answer}
                if isinstance(answer, list)
                else ({str(answer)} if answer not in (None, "") else set())
            )
            item["options"] = [
                {
                    "id": str(option.get("id") or ""),
                    "text": str(option.get("text") or ""),
                    "selected": str(option.get("id") or "") in selected,
                    "correct": bool(option.get("correct")),
                }
                for option in question.get("options", [])
                if isinstance(option, dict)
            ]
        result.append(item)
    return result


def ensure_error_review_schema(db_path: str | Path) -> None:
    with transaction(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS jackside_error_review_settings (
                campaign_code TEXT PRIMARY KEY,
                price_jc INTEGER NOT NULL DEFAULT 30
                    CHECK(price_jc BETWEEN 0 AND 100000),
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS quiz_question_explanations (
                question_id INTEGER PRIMARY KEY
                    REFERENCES quiz_questions(id) ON DELETE CASCADE,
                explanation TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS jackside_error_review_snapshots (
                submission_id INTEGER PRIMARY KEY
                    REFERENCES quiz_submissions(id) ON DELETE CASCADE,
                client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                campaign_code TEXT NOT NULL,
                price_jc INTEGER NOT NULL CHECK(price_jc BETWEEN 0 AND 100000),
                eligible INTEGER NOT NULL DEFAULT 0 CHECK(eligible IN (0,1)),
                questions_snapshot_json TEXT NOT NULL DEFAULT '[]',
                answers_json TEXT NOT NULL DEFAULT '{}',
                purchased_at TEXT,
                purchase_ledger_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS ix_error_review_snapshot_client_campaign
                ON jackside_error_review_snapshots(client_id, campaign_code, submission_id DESC);

            CREATE TABLE IF NOT EXISTS jackside_error_review_snapshot_explanations (
                submission_id INTEGER NOT NULL
                    REFERENCES jackside_error_review_snapshots(submission_id) ON DELETE CASCADE,
                question_code TEXT NOT NULL,
                explanation TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(submission_id, question_code)
            );

            DROP TRIGGER IF EXISTS trg_jackside_error_review_snapshot;
            CREATE TRIGGER trg_jackside_error_review_snapshot
            AFTER INSERT ON quiz_submissions
            WHEN EXISTS (
                SELECT 1 FROM quiz_campaigns qc
                WHERE qc.code=NEW.campaign_code
                  AND qc.campaign_type='daily_414'
            )
            BEGIN
                INSERT OR IGNORE INTO jackside_error_review_snapshots(
                    submission_id, client_id, campaign_code, price_jc, eligible,
                    questions_snapshot_json, answers_json
                )
                VALUES (
                    NEW.id,
                    NEW.client_id,
                    NEW.campaign_code,
                    COALESCE(
                        (SELECT price_jc FROM jackside_error_review_settings
                         WHERE campaign_code=NEW.campaign_code),
                        30
                    ),
                    CASE
                        WHEN IFNULL(NEW.main_round_completed,1)=1
                         AND IFNULL(NEW.main_prize_eligible,0)=0
                         AND IFNULL(NEW.max_correct_count,0)>0
                         AND IFNULL(NEW.correct_count,0)<IFNULL(NEW.max_correct_count,0)
                        THEN 1 ELSE 0
                    END,
                    COALESCE(NEW.questions_snapshot_json, '[]'),
                    COALESCE(NEW.answers_json, '{}')
                );

                INSERT OR REPLACE INTO jackside_error_review_snapshot_explanations(
                    submission_id, question_code, explanation
                )
                SELECT NEW.id, qq.code, COALESCE(qe.explanation, '')
                FROM quiz_questions qq
                LEFT JOIN quiz_question_explanations qe ON qe.question_id=qq.id
                WHERE qq.campaign_code=NEW.campaign_code
                  AND IFNULL(qq.game_round,'main')='main';
            END;
            """
        )

        conn.execute(
            """
            INSERT OR IGNORE INTO jackside_error_review_snapshots(
                submission_id, client_id, campaign_code, price_jc, eligible,
                questions_snapshot_json, answers_json, created_at
            )
            SELECT qs.id, qs.client_id, qs.campaign_code,
                   COALESCE(s.price_jc, ?),
                   CASE
                       WHEN IFNULL(qs.main_round_completed,1)=1
                        AND IFNULL(qs.main_prize_eligible,0)=0
                        AND IFNULL(qs.max_correct_count,0)>0
                        AND IFNULL(qs.correct_count,0)<IFNULL(qs.max_correct_count,0)
                       THEN 1 ELSE 0
                   END,
                   COALESCE(qs.questions_snapshot_json, '[]'),
                   COALESCE(qs.answers_json, '{}'),
                   qs.created_at
            FROM quiz_submissions qs
            JOIN quiz_campaigns qc ON qc.code=qs.campaign_code
            LEFT JOIN jackside_error_review_settings s ON s.campaign_code=qs.campaign_code
            WHERE qc.campaign_type='daily_414'
            """,
            (DEFAULT_ERROR_REVIEW_PRICE_JC,),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO jackside_error_review_snapshot_explanations(
                submission_id, question_code, explanation
            )
            SELECT ers.submission_id, qq.code, COALESCE(qe.explanation, '')
            FROM jackside_error_review_snapshots ers
            JOIN quiz_questions qq ON qq.campaign_code=ers.campaign_code
            LEFT JOIN quiz_question_explanations qe ON qe.question_id=qq.id
            WHERE IFNULL(qq.game_round,'main')='main'
            """
        )


def _asset_response(response: Any, asset_path: str) -> Any:
    if not hasattr(response, "body"):
        return response
    content_type = str(response.headers.get("content-type") or "")
    if "text/html" not in content_type:
        return response
    body = bytes(response.body).decode("utf-8")
    if asset_path in body or "</body>" not in body:
        return response
    html = body.replace(
        "</body>",
        f'<script src="{asset_path}" defer></script></body>',
        1,
    )
    headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in {"content-length", "content-type"}
    }
    return HTMLResponse(
        html,
        status_code=int(getattr(response, "status_code", 200) or 200),
        headers=headers,
    )


def _wrap_html_asset(app: FastAPI, *, path: str, asset_path: str) -> None:
    route = next(
        (
            candidate
            for candidate in app.routes
            if isinstance(candidate, APIRoute)
            and candidate.path == path
            and "GET" in (candidate.methods or set())
        ),
        None,
    )
    if route is None:
        return
    marker = f"_error_review_asset_{asset_path}"
    if getattr(route, marker, False):
        return
    original_endpoint = route.dependant.call

    async def asset_wrapper(**kwargs: Any):
        result = original_endpoint(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        return _asset_response(result, asset_path)

    route.endpoint = asset_wrapper
    route.dependant.call = asset_wrapper
    setattr(route, marker, True)


def _current_review_snapshot(
    conn: sqlite3.Connection,
    *,
    client_id: int,
    campaign: str,
    submission_id: int | None = None,
) -> sqlite3.Row | None:
    params: list[Any] = [int(client_id), normalize_campaign(campaign)]
    extra = ""
    if submission_id is not None:
        extra = " AND ers.submission_id=?"
        params.append(int(submission_id))
    return conn.execute(
        f"""
        SELECT ers.*, qs.correct_count, qs.max_correct_count,
               qs.main_prize_eligible, qs.main_round_completed
        FROM jackside_error_review_snapshots ers
        JOIN quiz_submissions qs ON qs.id=ers.submission_id
        WHERE ers.client_id=? AND ers.campaign_code=? {extra}
        ORDER BY ers.submission_id DESC
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()


def install_jackside_error_review(app: FastAPI) -> FastAPI:
    if getattr(app.state, "jackside_error_review_installed", False):
        return app
    app.state.jackside_error_review_installed = True
    settings = app.state.settings
    ensure_error_review_schema(settings.db_path)

    @app.get("/api/quiz/error-review/status", response_class=JSONResponse)
    async def error_review_status(request: Request, campaign: str = "default"):
        member = _current_member(request, required=True)
        client_id = int(member["client_id"])
        campaign_code = normalize_campaign(campaign)
        with connect(settings.db_path) as conn:
            snapshot = _current_review_snapshot(
                conn,
                client_id=client_id,
                campaign=campaign_code,
            )
            balance = jackcoin_balance(conn, client_id)
        if not snapshot:
            return {
                "eligible": False,
                "purchased": False,
                "campaign": campaign_code,
                "balance_jc": balance,
                "reason": "no_completed_submission",
            }
        wrong_count = max(
            0,
            int(snapshot["max_correct_count"] or 0)
            - int(snapshot["correct_count"] or 0),
        )
        price = int(snapshot["price_jc"] or 0)
        purchased = bool(snapshot["purchased_at"])
        eligible = bool(snapshot["eligible"])
        return {
            "eligible": eligible,
            "purchased": purchased,
            "campaign": campaign_code,
            "submission_id": int(snapshot["submission_id"]),
            "price_jc": price,
            "balance_jc": balance,
            "wrong_count": wrong_count,
            "can_purchase": bool(eligible and not purchased and balance >= price),
            "reason": "ok" if eligible else "not_eligible",
        }

    @app.post("/api/quiz/error-review/purchase", response_class=JSONResponse)
    async def purchase_error_review(request: Request):
        member = _current_member(request, required=True)
        client_id = int(member["client_id"])
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(status_code=400, detail="Некорректные данные")
        campaign_code = normalize_campaign(payload.get("campaign"))
        try:
            submission_id = int(payload.get("submission_id") or 0)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="Некорректный результат квиза") from exc
        if submission_id <= 0:
            raise HTTPException(status_code=422, detail="Некорректный результат квиза")

        with transaction(settings.db_path) as conn:
            snapshot = _current_review_snapshot(
                conn,
                client_id=client_id,
                campaign=campaign_code,
                submission_id=submission_id,
            )
            if not snapshot or not bool(snapshot["eligible"]):
                raise HTTPException(status_code=403, detail="Разбор для этого результата недоступен")
            if snapshot["purchased_at"]:
                return {
                    "ok": True,
                    "purchased": True,
                    "already_purchased": True,
                    "submission_id": submission_id,
                    "price_jc": int(snapshot["price_jc"] or 0),
                    "balance_jc": jackcoin_balance(conn, client_id),
                }

            price = int(snapshot["price_jc"] or 0)
            balance = jackcoin_balance(conn, client_id)
            if balance < price:
                raise HTTPException(
                    status_code=409,
                    detail=f"Недостаточно JACKCOIN: нужно {price} JC, доступно {balance} JC",
                )

            ledger_id = None
            if price > 0:
                idempotency_key = f"quiz:error-review:{submission_id}"
                existing = conn.execute(
                    "SELECT id FROM jackcoin_ledger WHERE idempotency_key=?",
                    (idempotency_key,),
                ).fetchone()
                if existing:
                    ledger_id = int(existing["id"])
                else:
                    cursor = conn.execute(
                        """
                        INSERT INTO jackcoin_ledger(
                            client_id, amount, operation_type, source_type,
                            source_id, idempotency_key, comment
                        ) VALUES (?, ?, 'spend', 'quiz_error_review', ?, ?, ?)
                        """,
                        (
                            client_id,
                            -price,
                            str(submission_id),
                            idempotency_key,
                            f"JACKSIDE: разбор ошибок за {price} JC",
                        ),
                    )
                    ledger_id = int(cursor.lastrowid)
            conn.execute(
                """
                UPDATE jackside_error_review_snapshots
                SET purchased_at=COALESCE(purchased_at, CURRENT_TIMESTAMP),
                    purchase_ledger_id=COALESCE(purchase_ledger_id, ?)
                WHERE submission_id=? AND client_id=?
                """,
                (ledger_id, submission_id, client_id),
            )
            balance_after = jackcoin_balance(conn, client_id)
        return {
            "ok": True,
            "purchased": True,
            "already_purchased": False,
            "submission_id": submission_id,
            "price_jc": price,
            "balance_jc": balance_after,
        }

    @app.get("/api/quiz/error-review", response_class=JSONResponse)
    async def error_review_data(
        request: Request,
        campaign: str = "default",
        submission_id: int = 0,
    ):
        member = _current_member(request, required=True)
        client_id = int(member["client_id"])
        campaign_code = normalize_campaign(campaign)
        with connect(settings.db_path) as conn:
            snapshot = _current_review_snapshot(
                conn,
                client_id=client_id,
                campaign=campaign_code,
                submission_id=submission_id if submission_id > 0 else None,
            )
            if not snapshot or not bool(snapshot["eligible"]):
                raise HTTPException(status_code=404, detail="Разбор не найден")
            if not snapshot["purchased_at"]:
                raise HTTPException(status_code=402, detail="Сначала откройте разбор за JACKCOIN")
            explanation_rows = conn.execute(
                """
                SELECT question_code, explanation
                FROM jackside_error_review_snapshot_explanations
                WHERE submission_id=?
                """,
                (int(snapshot["submission_id"]),),
            ).fetchall()
            explanations = {
                str(row["question_code"]): str(row["explanation"] or "")
                for row in explanation_rows
            }
            balance = jackcoin_balance(conn, client_id)
        questions = _review_payload(snapshot, explanations)
        return {
            "ok": True,
            "campaign": campaign_code,
            "submission_id": int(snapshot["submission_id"]),
            "price_jc": int(snapshot["price_jc"] or 0),
            "balance_jc": balance,
            "questions": questions,
            "wrong_count": len(questions),
        }

    @app.get(
        "/api/master/quiz-campaigns/{campaign_id}/error-review-config",
        response_class=JSONResponse,
    )
    async def admin_error_review_config(request: Request, campaign_id: int):
        _require_master(request)
        with connect(settings.db_path) as conn:
            campaign = conn.execute(
                "SELECT id,code,campaign_type FROM quiz_campaigns WHERE id=?",
                (campaign_id,),
            ).fetchone()
            if not campaign:
                raise HTTPException(status_code=404, detail="Кампания не найдена")
            setting = conn.execute(
                "SELECT price_jc FROM jackside_error_review_settings WHERE campaign_code=?",
                (campaign["code"],),
            ).fetchone()
            rows = conn.execute(
                """
                SELECT qq.id, qq.title, qq.game_round,
                       COALESCE(qe.explanation,'') AS explanation
                FROM quiz_questions qq
                LEFT JOIN quiz_question_explanations qe ON qe.question_id=qq.id
                WHERE qq.campaign_code=?
                ORDER BY qq.id
                """,
                (campaign["code"],),
            ).fetchall()
        return {
            "campaign_id": int(campaign["id"]),
            "campaign_code": campaign["code"],
            "campaign_type": campaign["campaign_type"],
            "price_jc": int(setting["price_jc"]) if setting else DEFAULT_ERROR_REVIEW_PRICE_JC,
            "questions": [
                {
                    "id": int(row["id"]),
                    "title": row["title"],
                    "game_round": row["game_round"],
                    "explanation": row["explanation"],
                }
                for row in rows
            ],
        }

    @app.post(
        "/api/master/quiz-campaigns/{campaign_id}/error-review-price",
        response_class=JSONResponse,
    )
    async def admin_error_review_price(request: Request, campaign_id: int):
        _require_master(request)
        try:
            payload = await request.json()
            price_jc = int(payload.get("price_jc"))
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="Проверьте стоимость разбора") from exc
        _check_csrf(request, str(payload.get("csrf_token") or ""))
        if not 0 <= price_jc <= MAX_ERROR_REVIEW_PRICE_JC:
            raise HTTPException(
                status_code=422,
                detail=f"Стоимость должна быть от 0 до {MAX_ERROR_REVIEW_PRICE_JC} JC",
            )
        with transaction(settings.db_path) as conn:
            campaign = conn.execute(
                "SELECT id,code,campaign_type FROM quiz_campaigns WHERE id=?",
                (campaign_id,),
            ).fetchone()
            if not campaign:
                raise HTTPException(status_code=404, detail="Кампания не найдена")
            if campaign["campaign_type"] != "daily_414":
                raise HTTPException(status_code=422, detail="Разбор ошибок доступен только для JACKSIDE")
            conn.execute(
                """
                INSERT INTO jackside_error_review_settings(campaign_code,price_jc,updated_at)
                VALUES (?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(campaign_code) DO UPDATE SET
                    price_jc=excluded.price_jc,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (campaign["code"], price_jc),
            )
            conn.execute(
                """
                INSERT INTO admin_audit_log(
                    admin_id,admin_name,action,entity_type,entity_id,details
                ) VALUES (?,?,?,?,?,?)
                """,
                (
                    int(request.session["admin_id"]),
                    str(request.session.get("admin_name") or ""),
                    "update_error_review_price",
                    "quiz_campaign",
                    campaign_id,
                    json.dumps({"price_jc": price_jc}, ensure_ascii=False),
                ),
            )
        return {"ok": True, "price_jc": price_jc}

    @app.post(
        "/api/master/quiz-questions/{question_id}/error-review-explanation",
        response_class=JSONResponse,
    )
    async def admin_error_review_explanation(request: Request, question_id: int):
        _require_master(request)
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="Некорректные данные") from exc
        _check_csrf(request, str(payload.get("csrf_token") or ""))
        explanation = str(payload.get("explanation") or "").strip()
        if len(explanation) > MAX_EXPLANATION_LENGTH:
            raise HTTPException(
                status_code=422,
                detail=f"Комментарий должен быть не длиннее {MAX_EXPLANATION_LENGTH} символов",
            )
        with transaction(settings.db_path) as conn:
            question = conn.execute(
                """
                SELECT qq.id,qq.title,qq.campaign_code,qc.campaign_type
                FROM quiz_questions qq
                JOIN quiz_campaigns qc ON qc.code=qq.campaign_code
                WHERE qq.id=?
                """,
                (question_id,),
            ).fetchone()
            if not question:
                raise HTTPException(status_code=404, detail="Вопрос не найден")
            if question["campaign_type"] != "daily_414":
                raise HTTPException(status_code=422, detail="Комментарий разбора доступен только для JACKSIDE")
            conn.execute(
                """
                INSERT INTO quiz_question_explanations(question_id,explanation,updated_at)
                VALUES (?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(question_id) DO UPDATE SET
                    explanation=excluded.explanation,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (question_id, explanation),
            )
            conn.execute(
                """
                INSERT INTO admin_audit_log(
                    admin_id,admin_name,action,entity_type,entity_id,details
                ) VALUES (?,?,?,?,?,?)
                """,
                (
                    int(request.session["admin_id"]),
                    str(request.session.get("admin_name") or ""),
                    "update_error_review_explanation",
                    "quiz_question",
                    question_id,
                    json.dumps(
                        {"campaign": question["campaign_code"], "length": len(explanation)},
                        ensure_ascii=False,
                    ),
                ),
            )
        return {"ok": True, "question_id": question_id, "explanation": explanation}

    _wrap_html_asset(app, path="/quiz", asset_path=MEMBER_ASSET)
    _wrap_html_asset(
        app,
        path="/master/quiz-builder/{campaign_id:int}",
        asset_path=ADMIN_ASSET,
    )
    return app


__all__ = [
    "DEFAULT_ERROR_REVIEW_PRICE_JC",
    "ensure_error_review_schema",
    "install_jackside_error_review",
]
