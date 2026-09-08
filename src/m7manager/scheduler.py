from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Asia/Shanghai")
TASKS = {"main", "daily", "power"}


def validate_time(value: str):
    try:
        hour, minute = map(int, value.split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59) or value != f"{hour:02}:{minute:02}":
            raise ValueError
        return hour, minute
    except (ValueError, AttributeError):
        raise ValueError("时间格式应为 HH:mm，例如 04:15") from None


def next_occurrence(local_time: str, now: float):
    hour, minute = validate_time(local_time)
    local = datetime.fromtimestamp(now, ZONE)
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate.timestamp() <= now:
        candidate += timedelta(days=1)
    return candidate.timestamp()


def tick(store, now: float):
    """Coalesce offline dates to the most recent occurrence, persist before advancing."""
    with store.transaction() as db:
        schedules = db.execute(
            """SELECT s.* FROM schedules s JOIN accounts a ON a.id=s.account_id
            WHERE s.enabled=1 AND a.enabled=1 AND a.blocked_reason='' AND s.next_run_at<=?""",
            (now,),
        ).fetchall()
        for schedule in schedules:
            latest = schedule["next_run_at"]
            days = int((now - latest) // 86400)
            latest += days * 86400  # Shanghai has no DST in the supported schedule period.
            key = f"schedule:{schedule['id']}:{schedule['revision']}:{latest:.0f}"
            expiry = latest + schedule["grace_seconds"]
            state = "QUEUED" if now <= expiry else "MISSED"
            db.execute(
                """INSERT OR IGNORE INTO triggers
                (id,account_id,schedule_id,task,source,scheduled_for,expires_at,idempotency_key,state,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    uuid.uuid4().hex,
                    schedule["account_id"],
                    schedule["id"],
                    schedule["task"],
                    "scheduled",
                    latest,
                    expiry,
                    key,
                    state,
                    now,
                ),
            )
            db.execute(
                "UPDATE schedules SET next_run_at=? WHERE id=?", (latest + 86400, schedule["id"])
            )
        db.execute(
            "UPDATE triggers SET state='MISSED' WHERE state='QUEUED' AND expires_at<?", (now,)
        )
