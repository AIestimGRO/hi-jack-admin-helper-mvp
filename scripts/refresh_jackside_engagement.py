from __future__ import annotations

import argparse

from app.db import init_db, transaction
from app.services.jackside_engagement import process_referral_qualification, refresh_member_engagement


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill JACKSIDE referrals, achievements and titles")
    parser.add_argument("db_path")
    parser.add_argument("--timezone", default="Europe/Moscow")
    args = parser.parse_args()
    init_db(args.db_path)
    with transaction(args.db_path) as conn:
        for row in conn.execute(
            "SELECT invited_client_id FROM referral_qualification_progress ORDER BY id"
        ).fetchall():
            submission = conn.execute(
                """SELECT qs.id FROM quiz_submissions qs JOIN quiz_campaigns qc ON qc.code=qs.campaign_code
                   WHERE qs.client_id=? AND qc.campaign_type='daily_414' AND IFNULL(qs.main_round_completed,1)=1
                   ORDER BY qs.created_at DESC,qs.id DESC LIMIT 1""",
                (int(row["invited_client_id"]),),
            ).fetchone()
            if submission:
                process_referral_qualification(
                    conn,
                    invited_client_id=int(row["invited_client_id"]),
                    submission_id=int(submission["id"]),
                    timezone_name=args.timezone,
                )
        clients = conn.execute("SELECT id FROM clients ORDER BY id").fetchall()
        for row in clients:
            refresh_member_engagement(
                conn, client_id=int(row["id"]), timezone_name=args.timezone
            )
    print(f"engagement refreshed for {len(clients)} clients")


if __name__ == "__main__":
    main()
