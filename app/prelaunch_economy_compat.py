from __future__ import annotations

import sqlite3

from fastapi import FastAPI

from app.db import transaction


def ensure_prelaunch_economy_compat(conn: sqlite3.Connection) -> None:
    """Rebuild launch-only triggers with legacy-safe and Unicode-safe guards."""
    from app.prelaunch_experience import _amount_sql, _jackside_referral_trigger_sql

    conn.executescript(
        """
        DROP TRIGGER IF EXISTS trg_prelaunch_daily_campaign_defaults;
        CREATE TRIGGER trg_prelaunch_daily_campaign_defaults
        AFTER INSERT ON quiz_campaigns
        WHEN NEW.campaign_type='daily_414' AND NEW.code LIKE 'jackside_%'
        BEGIN
            UPDATE quiz_campaigns
            SET jackcoin_per_correct=COALESCE(
                    (SELECT amount FROM jackcoin_economy_snapshots
                     WHERE entity_type='jackside' AND entity_id=NEW.code
                       AND setting_key='jackside_correct'),
                    (SELECT amount FROM jackcoin_economy_settings
                     WHERE setting_key='jackside_correct'),
                    NEW.jackcoin_per_correct
                ),
                jackcoin_completion_bonus=COALESCE(
                    (SELECT amount FROM jackcoin_economy_snapshots
                     WHERE entity_type='jackside' AND entity_id=NEW.code
                       AND setting_key='jackside_completion'),
                    (SELECT amount FROM jackcoin_economy_settings
                     WHERE setting_key='jackside_completion'),
                    NEW.jackcoin_completion_bonus
                ),
                jackcoin_perfect_bonus=COALESCE(
                    (SELECT amount FROM jackcoin_economy_snapshots
                     WHERE entity_type='jackside' AND entity_id=NEW.code
                       AND setting_key='jackside_perfect'),
                    (SELECT amount FROM jackcoin_economy_settings
                     WHERE setting_key='jackside_perfect'),
                    NEW.jackcoin_perfect_bonus
                ),
                updated_at=CURRENT_TIMESTAMP
            WHERE id=NEW.id;
        END;
        """
    )

    final_correct_amount = _amount_sql(
        "jackside_final_correct",
        entity_type="jackside",
        entity_id_sql="ft.campaign_code",
    )
    final_win_amount = _amount_sql(
        "jackside_final_win",
        entity_type="jackside",
        entity_id_sql="ft.campaign_code",
    )
    conn.executescript(
        f"""
        DROP TRIGGER IF EXISTS trg_prelaunch_final_correct_jc;
        CREATE TRIGGER trg_prelaunch_final_correct_jc
        AFTER INSERT ON daily_414_final_answers
        WHEN NEW.is_correct=1
        BEGIN
            INSERT OR IGNORE INTO jackcoin_ledger(
                client_id, amount, operation_type, source_type, source_id,
                idempotency_key, comment
            )
            SELECT df.client_id,
                   {final_correct_amount},
                   'earn', 'jackside_final_correct', CAST(NEW.id AS TEXT),
                   'jackside:final-correct:' || NEW.id,
                   'JACKSIDE: правильный ответ в суперфинале'
            FROM daily_414_finalists df
            JOIN daily_414_final_tables ft ON ft.id=df.final_table_id
            WHERE df.id=NEW.finalist_id
              AND ft.campaign_code LIKE 'jackside_%'
              AND ({final_correct_amount}) > 0;
        END;

        DROP TRIGGER IF EXISTS trg_prelaunch_final_win_jc;
        CREATE TRIGGER trg_prelaunch_final_win_jc
        AFTER UPDATE OF status ON daily_414_finalists
        WHEN NEW.status='winner' AND OLD.status<>'winner'
        BEGIN
            INSERT OR IGNORE INTO jackcoin_ledger(
                client_id, amount, operation_type, source_type, source_id,
                idempotency_key, comment
            )
            SELECT NEW.client_id,
                   {final_win_amount},
                   'earn', 'jackside_final_win', CAST(NEW.final_table_id AS TEXT),
                   'jackside:final-win:' || NEW.id,
                   'JACKSIDE: победа в суперфинале'
            FROM daily_414_final_tables ft
            WHERE ft.id=NEW.final_table_id
              AND ft.campaign_code LIKE 'jackside_%'
              AND ({final_win_amount}) > 0;
        END;

        DROP TRIGGER IF EXISTS trg_prelaunch_final_win_insert_jc;
        CREATE TRIGGER trg_prelaunch_final_win_insert_jc
        AFTER INSERT ON daily_414_finalists
        WHEN NEW.status='winner'
        BEGIN
            INSERT OR IGNORE INTO jackcoin_ledger(
                client_id, amount, operation_type, source_type, source_id,
                idempotency_key, comment
            )
            SELECT NEW.client_id,
                   {final_win_amount},
                   'earn', 'jackside_final_win', CAST(NEW.final_table_id AS TEXT),
                   'jackside:final-win:' || NEW.id,
                   'JACKSIDE: победа в суперфинале'
            FROM daily_414_final_tables ft
            WHERE ft.id=NEW.final_table_id
              AND ft.campaign_code LIKE 'jackside_%'
              AND ({final_win_amount}) > 0;
        END;

        DROP TRIGGER IF EXISTS trg_prelaunch_jackside_referral_jc;
        CREATE TRIGGER trg_prelaunch_jackside_referral_jc
        AFTER INSERT ON quiz_submissions
        WHEN IFNULL(NEW.main_round_completed,1)=1
         AND NEW.max_correct_count>0
         AND NEW.campaign_code LIKE 'jackside_%'
         AND EXISTS (
             SELECT 1 FROM quiz_campaigns qc
             WHERE qc.code=NEW.campaign_code AND qc.campaign_type='daily_414'
         )
        BEGIN
            {_jackside_referral_trigger_sql(1)}
            {_jackside_referral_trigger_sql(2)}
            {_jackside_referral_trigger_sql(3)}
        END;
        """
    )

    has_hijack = conn.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='hi_jack_rating_imports'
        """
    ).fetchone()
    if not has_hijack:
        return

    conn.executescript(
        """
        DROP TRIGGER IF EXISTS trg_prelaunch_hijack_import_snapshot;
        CREATE TRIGGER trg_prelaunch_hijack_import_snapshot
        AFTER INSERT ON hi_jack_rating_imports
        BEGIN
            UPDATE hi_jack_rating_imports
            SET tournament_type=CASE
                WHEN NEW.tournament_name LIKE '%финал%'
                  OR NEW.tournament_name LIKE '%Финал%'
                  OR NEW.tournament_name LIKE '%ФИНАЛ%'
                  OR lower(NEW.tournament_name) LIKE '%final%'
                THEN 'final' ELSE 'regular' END
            WHERE id=NEW.id;

            INSERT OR IGNORE INTO jackcoin_economy_snapshots(
                entity_type, entity_id, setting_key, amount
            )
            SELECT 'hijack', CAST(NEW.id AS TEXT), setting_key, amount
            FROM jackcoin_economy_settings
            WHERE setting_key LIKE 'hijack_%'
               OR setting_key LIKE 'ref_hijack_%';
        END;
        """
    )


def install_prelaunch_economy_compat(app: FastAPI) -> FastAPI:
    if getattr(app.state, "prelaunch_economy_compat_installed", False):
        return app
    app.state.prelaunch_economy_compat_installed = True
    with transaction(app.state.settings.db_path) as conn:
        ensure_prelaunch_economy_compat(conn)
    return app


__all__ = ["ensure_prelaunch_economy_compat", "install_prelaunch_economy_compat"]
