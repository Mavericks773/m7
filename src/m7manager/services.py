from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from . import scheduler
from .config import (
    account_dir,
    apply_pending,
    atomic_text,
    dump_config,
    initialize,
    load_config,
    redacted_config,
    validate_patch,
)
from .docker_runtime import ACCOUNT, DIGEST, RUN, DockerRuntime, NotFound
from .dungeon_catalog import catalog_metadata, dungeon_types
from .dungeon_config import (
    dungeon_fingerprint,
    dungeon_summary,
    merge_config_patch,
    validate_manual_power,
    weekly_patch,
)
from .storage import ACTIVE, ACTIVE_SQL, Store
from .upstream_adapter import classify


class Manager:
    """Single-writer application service. Invoke from the UI's worker thread only."""

    def __init__(self, root: Path, runtime=None, clock=time.time):
        self.store = Store(root)
        self.root = self.store.root
        self.clock = clock
        self.runtime = runtime or DockerRuntime(self.store.setting("installation_id"))
        self.docker_status = "尚未检查 Docker"
        self.alerts = []
        self.shutting_down = False

    def add_account(self, name):
        name = name.strip()
        if not name or len(name) > 40:
            raise ValueError("账号名称须为 1～40 个字符")
        account_id = uuid.uuid4().hex
        now = self.clock()
        with self.store.transaction() as db:
            count = db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
            if count >= 2:
                raise ValueError("当前版本最多管理两个账号")
            initialize(self.root, account_id)
            db.execute(
                "INSERT INTO accounts (id,display_name,created_at) VALUES (?,?,?)",
                (account_id, name, now),
            )
            local_time = "04:15" if count == 0 else "04:20"
            db.execute(
                "INSERT INTO schedules (id,account_id,local_time,next_run_at) VALUES (?,?,?,?)",
                (
                    uuid.uuid4().hex,
                    account_id,
                    local_time,
                    scheduler.next_occurrence(local_time, now),
                ),
            )
        return account_id

    def account(self, account_id):
        row = self.store.one("SELECT * FROM accounts WHERE id=?", (account_id,))
        if not row:
            raise ValueError("账号不存在")
        return row

    def edit_account(
        self,
        account_id,
        name,
        enabled,
        local_time,
        scheduled,
        timeout,
        patch,
        expected_dungeon_version=None,
    ):
        self.account(account_id)
        scheduler.validate_time(local_time)
        validate_patch(patch)
        if not name.strip() or len(name.strip()) > 40 or not 60 <= timeout <= 14400:
            raise ValueError("请检查名称和运行超时（1～240 分钟）")
        with self.store.transaction() as db:
            account = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            pending = json.loads(account["pending_config"])
            dungeon_fields = {
                "instance_type",
                "instance_names",
                "instance_names_challenge_count",
                "power_plan",
                "power_plan_keep",
                "echo_of_war_enable",
                "echo_of_war_start_day_of_week",
                "build_target_enable",
            }
            if dungeon_fields & patch.keys():
                config = load_config(account_dir(self.root, account_id) / "config.yaml")
                current = merge_config_patch(config, pending)
                plan_edit = bool({"power_plan", "power_plan_keep"} & patch.keys())
                if plan_edit and (
                    current.get("power_plan")
                    or patch.get("power_plan")
                    or any(
                        current.get(k) != patch[k]
                        for k in ("power_plan", "power_plan_keep")
                        if k in patch
                    )
                ):
                    if any(r["account_id"] == account_id for r in self.store.active()):
                        raise ValueError("账号运行期间不能编辑或清空体力计划，请停止任务后再修改")
                    if expected_dungeon_version is None:
                        raise ValueError("修改体力计划需要最新配置版本，请重新打开设置")
                if (
                    expected_dungeon_version is not None
                    and expected_dungeon_version != dungeon_fingerprint(current)
                ):
                    raise ValueError("副本配置已在设置窗口打开后变化，请重新打开设置后再保存")
                merged = merge_config_patch(current, patch)
                ordinary_names = set(patch.get("instance_names", {})) - {"历战余响"}
                if (
                    ordinary_names
                    or {
                        "instance_type",
                        "instance_names_challenge_count",
                        "power_plan",
                        "power_plan_keep",
                    }
                    & patch.keys()
                ):
                    validate_manual_power(merged)
                if merged.get("echo_of_war_enable") and (
                    {"echo_of_war_enable", "echo_of_war_start_day_of_week"} & patch.keys()
                    or "历战余响" in patch.get("instance_names", {})
                ):
                    weekly_patch(
                        True,
                        merged.get("echo_of_war_start_day_of_week"),
                        merged.get("instance_names", {}).get("历战余响"),
                    )
                    if merged.get("build_target_enable"):
                        raise ValueError("手选周本需关闭培养目标，避免覆盖周本选择")
            pending = merge_config_patch(pending, patch)
            db.execute(
                """UPDATE accounts SET display_name=?,enabled=?,timeout_seconds=?,pending_config=?
                          WHERE id=?""",
                (name.strip(), int(enabled), timeout, json.dumps(pending), account_id),
            )
            old = db.execute("SELECT * FROM schedules WHERE account_id=?", (account_id,)).fetchone()
            if old["local_time"] != local_time or bool(old["enabled"]) != bool(scheduled):
                db.execute(
                    "UPDATE triggers SET state='CANCELLED' WHERE schedule_id=? AND state='QUEUED'",
                    (old["id"],),
                )
                db.execute(
                    """UPDATE schedules SET local_time=?,enabled=?,revision=revision+1,next_run_at=?
                              WHERE account_id=?""",
                    (
                        local_time,
                        int(scheduled),
                        scheduler.next_occurrence(local_time, self.clock()),
                        account_id,
                    ),
                )
            if not enabled:
                db.execute(
                    "UPDATE triggers SET state='CANCELLED' WHERE account_id=? AND state='QUEUED'",
                    (account_id,),
                )

    def set_concurrency(self, value):
        if type(value) is not int or value not in (1, 2):
            raise ValueError("并发数只能为 1 或 2")
        self.store.set_setting("concurrency", value)

    def prepare_image(self):
        if self.store.active():
            raise ValueError("请等待当前任务结束后准备镜像")
        digest = self.runtime.prepare_image(self.store.setting("image_digest", ""))
        self.set_image(digest)
        return digest

    def set_image(self, digest):
        if not DIGEST.fullmatch(digest):
            raise ValueError("只接受官方镜像的完整 sha256 digest")
        self.runtime.connect()
        self.runtime.ensure_image(digest)
        self.store.set_setting("image_digest", digest)
        self.store.execute("UPDATE accounts SET image_digest=?", (digest,))

    def enqueue(self, account_id, task="main", request_id=None):
        if self.shutting_down:
            raise ValueError("程序正在退出")
        account = self.account(account_id)
        if not account["enabled"]:
            raise ValueError("请先启用该账号")
        if task not in scheduler.TASKS:
            raise ValueError("当前版本仅支持完整日常、每日实训和清体力")
        image = account["image_digest"] or self.store.setting("image_digest", "")
        if not DIGEST.fullmatch(image):
            raise ValueError("请先点击“准备官方镜像”")
        self.runtime.connect()
        now = self.clock()
        trigger_id = uuid.uuid4().hex
        key = f"manual:{request_id or trigger_id}"
        with self.store.transaction() as db:
            existing = db.execute(
                "SELECT id FROM triggers WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing:
                return existing["id"]
            existing = db.execute(
                f"SELECT trigger_id FROM runs WHERE account_id=? AND state IN ({ACTIVE_SQL})",
                (account_id,),
            ).fetchone()
            if existing:
                return existing["trigger_id"]
            existing = db.execute(
                "SELECT id FROM triggers WHERE account_id=? AND state='QUEUED' AND source='manual'",
                (account_id,),
            ).fetchone()
            if existing:
                return existing["id"]
            db.execute(
                """INSERT INTO triggers
                (id,account_id,task,source,scheduled_for,expires_at,idempotency_key,created_at)
                VALUES (?,?,?,?,?,?,?,?)""",
                (trigger_id, account_id, task, "manual", now, now + 7200, key, now),
            )
        return trigger_id

    def cancel(self, account_id):
        self.account(account_id)
        with self.store.transaction() as db:
            db.execute(
                "UPDATE triggers SET state='CANCELLED' WHERE account_id=? AND state='QUEUED'",
                (account_id,),
            )
            db.execute(
                f"UPDATE runs SET stop_reason='CANCELLED',state='STOPPING' WHERE account_id=? AND state IN ({ACTIVE_SQL})",
                (account_id,),
            )

    def _claim(self, now):
        with self.store.transaction() as db:
            active = db.execute(
                f"SELECT account_id FROM runs WHERE state IN ({ACTIVE_SQL})"
            ).fetchall()
            if len(active) >= self.store.setting("concurrency", 1):
                return None
            row = db.execute(
                f"""SELECT t.*,a.timeout_seconds,a.image_digest,a.blocked_reason
                FROM triggers t JOIN accounts a ON a.id=t.account_id
                WHERE t.state='QUEUED' AND a.enabled=1 AND t.expires_at>=?
                AND (t.source='manual' OR a.blocked_reason='')
                AND a.id NOT IN (SELECT account_id FROM runs WHERE state IN ({ACTIVE_SQL}))
                ORDER BY t.scheduled_for,t.created_at,t.rowid LIMIT 1""",
                (now,),
            ).fetchone()
            if not row:
                return None
            digest = row["image_digest"] or self.store.setting("image_digest", "")
            if not DIGEST.fullmatch(digest):
                return None
            run_id = uuid.uuid4().hex
            db.execute(
                """INSERT INTO runs
                (id,trigger_id,account_id,task,state,image_digest,timeout_seconds,created_at)
                VALUES (?,?,?,?,'PREPARING',?,?,?)""",
                (
                    run_id,
                    row["id"],
                    row["account_id"],
                    row["task"],
                    digest,
                    row["timeout_seconds"],
                    now,
                ),
            )
            db.execute("UPDATE triggers SET state='RUNNING' WHERE id=?", (row["id"],))
        return self.store.one("SELECT * FROM runs WHERE id=?", (run_id,))

    def _update_run(self, run_id, **values):
        columns = {row["name"] for row in self.store.rows("PRAGMA table_info(runs)")}
        if not values.keys() <= columns:
            raise ValueError("Unknown run field")
        self.store.execute(
            "UPDATE runs SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?",
            (*values.values(), run_id),
        )

    def _finish(self, run, state, process, error="", snap=None):
        # Conservative: the current adapter does not assert that every game task succeeded.
        with self.store.transaction() as db:
            db.execute(
                """UPDATE runs SET state=?,process_result=?,finished_at=?,error_code=?,
                exit_code=?,oom_killed=? WHERE id=?""",
                (
                    state,
                    process,
                    self.clock(),
                    error,
                    snap.exit_code if snap else None,
                    int(snap.oom_killed) if snap else 0,
                    run["id"],
                ),
            )
            db.execute("UPDATE triggers SET state=? WHERE id=?", (state, run["trigger_id"]))
        result = self.store.one("SELECT * FROM runs WHERE id=?", (run["id"],))
        try:
            atomic_text(
                self.root / "runs" / run["id"] / "result.json",
                json.dumps(result, ensure_ascii=False, indent=2),
            )
        except OSError:
            self.alerts.append("运行结果已保存至数据库，但结果文件写入失败，请检查磁盘。")

    def _begin(self, run, snapshots):
        for old in snapshots:
            if old.labels.get(ACCOUNT) != run["account_id"]:
                continue
            previous = self.store.one("SELECT * FROM runs WHERE id=?", (old.labels.get(RUN),))
            if (
                old.status not in ("exited", "dead", "created")
                or not previous
                or previous["state"] in ("PREPARING", "STARTING")
            ):
                raise ValueError("发现未协调的旧容器，不能覆盖账号数据")
            self.runtime.remove(old.id, run["account_id"], old.labels[RUN])
        account = self.account(run["account_id"])
        config = apply_pending(self.root, account, run["id"])
        atomic_text(
            self.root / "runs" / run["id"] / "config.redacted.yaml",
            dump_config(redacted_config(config)),
        )
        dungeon = dungeon_summary(config)
        dungeon.update(catalog_metadata())
        dungeon["image_digest"] = run["image_digest"]
        atomic_text(
            self.root / "runs" / run["id"] / "dungeon-config.json",
            json.dumps(dungeon, ensure_ascii=False, indent=2),
        )
        self.store.execute("UPDATE accounts SET pending_config='{}' WHERE id=?", (account["id"],))
        container_id = self.runtime.create(run, account_dir(self.root, account["id"]))
        self._update_run(run["id"], container_id=container_id, state="STARTING")
        now = self.clock()
        self.runtime.start(container_id, account["id"], run["id"])
        self._update_run(
            run["id"], started_at=now, deadline_at=now + run["timeout_seconds"], state="RUNNING"
        )

    def _capture(self, run, final=False):
        stdout, stderr = self.runtime.logs(run["container_id"], run["account_id"], run["id"], final)
        path = self.root / "runs" / run["id"]
        atomic_text(path / "stdout.log", stdout)
        atomic_text(path / "stderr.log", stderr)
        # Docker timestamps permit ordering separate stdout/stderr streams.
        evidence = classify("\n".join(sorted((stdout + "\n" + stderr).splitlines())))
        if evidence.authenticated:
            self.store.execute(
                "UPDATE accounts SET auth_state='READY',blocked_reason='' WHERE id=?",
                (run["account_id"],),
            )
        if evidence.block_reason:
            self.store.execute(
                "UPDATE accounts SET blocked_reason=? WHERE id=?",
                (evidence.block_reason, run["account_id"]),
            )
        if evidence.block_reason == "需要重新扫码登录":
            self.store.execute(
                "UPDATE accounts SET auth_state='REAUTH_REQUIRED' WHERE id=?", (run["account_id"],)
            )
        return evidence

    def _poll(self, run, snap, now):
        if snap is None:
            # Full successful listing is required before calling _poll. Missing may mean a
            # manually removed container; never infer success or automatically replay it.
            if run["state"] == "PREPARING" and not run["container_id"]:
                self._finish(run, "FAILED", "UNKNOWN", "INTERRUPTED_BEFORE_CREATE")
            else:
                self._finish(run, "FAILED", "UNKNOWN", "CONTAINER_MISSING")
            return
        if snap.labels.get(ACCOUNT) != run["account_id"] or (
            run["container_id"] and run["container_id"] != snap.id
        ):
            raise ValueError("容器与运行记录不匹配")
        if not run["container_id"]:
            self._update_run(run["id"], container_id=snap.id)
            run["container_id"] = snap.id
        if snap.status == "created":
            if run["stop_reason"]:
                self.runtime.remove(snap.id, run["account_id"], run["id"])
                self._finish(run, "CANCELLED", "CANCELLED")
                return
            trigger = self.store.one(
                "SELECT expires_at FROM triggers WHERE id=?", (run["trigger_id"],)
            )
            if now > trigger["expires_at"]:
                self.runtime.remove(snap.id, run["account_id"], run["id"])
                self._finish(run, "MISSED", "MISSED", "EXPIRED_BEFORE_START")
                return
            # Recover the create -> persist -> start window without creating a duplicate.
            self._update_run(run["id"], state="STARTING")
            self.runtime.start(snap.id, run["account_id"], run["id"])
            self._update_run(
                run["id"], state="RUNNING", started_at=now, deadline_at=now + run["timeout_seconds"]
            )
            return
        if not run["started_at"]:
            started = snap.started_at or now
            self._update_run(
                run["id"], started_at=started, deadline_at=started + run["timeout_seconds"]
            )
            run.update(started_at=started, deadline_at=started + run["timeout_seconds"])
        if snap.status in ("exited", "dead"):
            evidence = self._capture(run, final=True)
            reason = run["stop_reason"]
            if reason:
                self._finish(run, reason, reason, snap=snap)
            elif snap.oom_killed:
                self._finish(run, "FAILED", "OOM", "OUT_OF_MEMORY", snap)
            elif snap.exit_code != 0 or evidence.block_reason:
                self._finish(
                    run,
                    "FAILED",
                    "NONZERO_EXIT" if snap.exit_code != 0 else "NORMAL_EXIT",
                    evidence.block_reason or "UPSTREAM_EXIT",
                    snap,
                )
            else:
                self._finish(run, "EXITED", "NORMAL_EXIT", snap=snap)
            if evidence.phase == "WAITING_LOGIN":
                self.store.execute(
                    "UPDATE accounts SET auth_state='REAUTH_REQUIRED',blocked_reason='需要重新扫码登录' WHERE id=?",
                    (run["account_id"],),
                )
            return
        if run["stop_reason"] or now >= run["deadline_at"]:
            reason = run["stop_reason"] or "TIMED_OUT"
            self._update_run(run["id"], state="STOPPING", stop_reason=reason)
            self.runtime.stop(snap.id, run["account_id"], run["id"])
            return  # Finalize only after the next observed stopped state.
        if snap.status != "running":
            self._update_run(
                run["id"], state="RECONCILING", error_code="CONTAINER_" + snap.status.upper()
            )
            return
        evidence = self._capture(run)
        self._update_run(run["id"], state=evidence.phase, error_code="")

    def tick(self):
        now = self.clock()
        self.alerts = []
        if not self.shutting_down:
            scheduler.tick(self.store, now)
        try:
            self.runtime.connect()
            snapshots = self.runtime.list_owned()
            self.docker_status = "Docker 已连接 · Linux 容器"
        except Exception as exc:
            self.docker_status = str(exc)
            self.store.execute(f"UPDATE runs SET state='RECONCILING' WHERE state IN ({ACTIVE_SQL})")
            return
        by_run = {s.labels.get(RUN): s for s in snapshots}
        known = {r["id"] for r in self.store.rows("SELECT id FROM runs")}
        orphans = [s for s in snapshots if s.labels.get(RUN) not in known]
        if orphans:
            self.alerts.append(
                "发现没有历史记录的受管容器，已暂停新任务。请在 Docker Desktop 核查，不会自动删除。"
            )
        for run in self.store.active():
            try:
                self._poll(run, by_run.get(run["id"]), now)
            except Exception as exc:
                self._update_run(run["id"], state="RECONCILING", error_code=type(exc).__name__)
                self.alerts.append("任务状态需要协调：" + str(exc))
        if orphans or self.shutting_down:
            return
        while True:
            run = self._claim(now)
            if run is None:
                break
            try:
                self._begin(run, snapshots)
            except (ValueError, FileNotFoundError, NotFound) as exc:
                fresh = self.store.one("SELECT * FROM runs WHERE id=?", (run["id"],))
                if fresh["container_id"]:
                    self._update_run(run["id"], state="RECONCILING", error_code=type(exc).__name__)
                else:
                    self._finish(run, "FAILED", "PREPARATION_FAILED", str(exc))
            except Exception as exc:
                # An uncertain create/start response may have created a live container.
                # Keep the account occupied until the next successful inventory.
                self._update_run(run["id"], state="RECONCILING", error_code=type(exc).__name__)
                self.alerts.append("启动结果待确认：" + str(exc))

    def shutdown(self):
        self.shutting_down = True
        self.store.execute("UPDATE triggers SET state='CANCELLED' WHERE state='QUEUED'")
        self.store.execute(
            f"UPDATE runs SET stop_reason='CANCELLED',state='STOPPING' WHERE state IN ({ACTIVE_SQL})"
        )

    def snapshot(self):
        accounts = self.store.rows("SELECT * FROM accounts ORDER BY created_at")
        for account in accounts:
            account["schedule"] = self.store.one(
                "SELECT * FROM schedules WHERE account_id=?", (account["id"],)
            )
            account["run"] = self.store.one(
                "SELECT * FROM runs WHERE account_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
                (account["id"],),
            )
            account["queued"] = self.store.one(
                "SELECT COUNT(*) AS n FROM triggers WHERE account_id=? AND state='QUEUED'",
                (account["id"],),
            )["n"]
            path = account_dir(self.root, account["id"])
            try:
                current_config = load_config(path / "config.yaml")
                pending = json.loads(account["pending_config"])
                config = merge_config_patch(current_config, pending)
                account["config"] = {
                    k: config.get(k)
                    for k in (
                        "daily_enable",
                        "power_enable",
                        "build_target_enable",
                        "cloud_game_use_paid_time",
                        "cloud_game_max_queue_time",
                        "cloud_game_login_timeout",
                    )
                }
                account["dungeon"] = dungeon_summary(config)
                account["dungeon"]["version"] = dungeon_fingerprint(config)
                names = config.get("instance_names", {})
                counts = config.get("instance_names_challenge_count", {})
                account["dungeon"]["instance_names"] = {
                    key: names.get(key) for key in dungeon_types() if isinstance(names, dict)
                }
                account["dungeon"]["challenge_counts"] = {
                    key: counts.get(key) for key in dungeon_types() if isinstance(counts, dict)
                }
                account["dungeon"]["pending"] = bool(
                    {
                        "instance_type",
                        "instance_names",
                        "instance_names_challenge_count",
                        "build_target_enable",
                        "power_plan",
                        "power_plan_keep",
                        "echo_of_war_enable",
                        "echo_of_war_start_day_of_week",
                        "activity_gardenofplenty_enable",
                        "activity_realmofthestrange_enable",
                        "activity_planarfissure_enable",
                        "merge_immersifier",
                    }
                    & pending.keys()
                )
            except Exception:
                account["config"] = {}
                account["dungeon"] = {
                    "fixed_mode": False,
                    "instance_type": None,
                    "instance_name": None,
                    "batch_count": None,
                    "conflicts": ["config_error"],
                    "pending": False,
                }
            account["qr_bytes"] = b""
            run = account["run"]
            account["run_dungeon"] = None
            if run and run["state"] in ACTIVE:
                try:
                    account["run_dungeon"] = json.loads(
                        (self.root / "runs" / run["id"] / "dungeon-config.json").read_text(
                            encoding="utf-8"
                        )
                    )
                except (OSError, ValueError):
                    pass
            if run and run["state"] == "WAITING_LOGIN" and run["started_at"]:
                qr = path / "logs" / "qrcode_login.png"
                try:
                    stat = qr.stat()
                    if stat.st_mtime >= run["started_at"] and stat.st_size <= 2_000_000:
                        account["qr_bytes"] = qr.read_bytes()
                except OSError:
                    pass
            account["log_tail"] = ""
            if run:
                chunks = []
                for name in ("stdout.log", "stderr.log"):
                    try:
                        with (self.root / "runs" / run["id"] / name).open("rb") as stream:
                            stream.seek(0, 2)
                            stream.seek(max(0, stream.tell() - 40000))
                            chunks.append(stream.read().decode("utf-8", "replace"))
                    except OSError:
                        pass
                account["log_tail"] = "\n".join(chunks)
        return {
            "accounts": accounts,
            "docker_status": self.docker_status,
            "alerts": self.alerts,
            "image_digest": self.store.setting("image_digest", ""),
            "concurrency": self.store.setting("concurrency", 1),
            "history": self.store.rows("""SELECT r.*,a.display_name FROM runs r
                    JOIN accounts a ON a.id=r.account_id ORDER BY r.created_at DESC,r.rowid DESC LIMIT 100"""),
            "missed": self.store.one("SELECT COUNT(*) AS n FROM triggers WHERE state='MISSED'")[
                "n"
            ],
            "active_count": len(self.store.active()),
        }

    def export_diagnostics(self):
        """Allowlist metadata only. No logs, account names, paths, or credentials."""
        payload = {
            "app_version": "0.1.0",
            "image_digest": self.store.setting("image_digest", ""),
            "concurrency": self.store.setting("concurrency", 1),
            "runs": [],
        }
        fields = (
            "id",
            "state",
            "created_at",
            "started_at",
            "finished_at",
            "exit_code",
            "oom_killed",
            "process_result",
            "business_result",
        )
        for run in self.store.rows("SELECT * FROM runs ORDER BY created_at DESC LIMIT 100"):
            payload["runs"].append({k: run[k] for k in fields})
        path = self.root / "diagnostics" / f"report-{uuid.uuid4().hex[:8]}.json"
        atomic_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
        return str(path)

    def close(self):
        self.runtime.close()
        self.store.close()
