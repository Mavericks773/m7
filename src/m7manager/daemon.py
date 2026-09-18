from __future__ import annotations

import base64
import hashlib
import json
import signal
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .docker_runtime import DockerRuntime
from .ipc import PROTOCOL_VERSION, IPCServer
from .locking import DataDirLock
from .paths import AppPaths
from .services import Manager


class Daemon:
    def __init__(self, paths: AppPaths):
        self.paths = paths
        self.lock = DataDirLock(paths.data_dir)
        if not self.lock.acquire():
            raise RuntimeError(f"数据目录已被占用：{paths.data_dir}")
        self.manager = Manager(paths.data_dir)
        self.server = IPCServer(paths.runtime_dir / "control.sock")
        self.stop_event = threading.Event()
        self.jobs = ThreadPoolExecutor(max_workers=1, thread_name_prefix="m7-job")
        self.image_future = None
        self.image_job_id = None
        self._recover_jobs()

    def _recover_jobs(self):
        now = time.time()
        self.manager.store.execute(
            "UPDATE jobs SET state='INTERRUPTED',message='服务重启后待核查',updated_at=? WHERE state IN ('PENDING','RUNNING')",
            (now,),
        )

    def _response_error(self, code, message):
        return {"ok": False, "error": {"code": code, "message": message}}

    def _remembered_request(self, request_id, action, payload):
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        row = self.manager.store.one("SELECT * FROM requests WHERE request_id=?", (request_id,))
        if row:
            if row["action"] != action or row["payload_hash"] != digest:
                raise ValueError("相同 request_id 不能携带不同参数")
            if row["state"] == "DONE":
                return json.loads(row["result_json"])
            if row["state"] == "FAILED":
                return {"ok": False, "error": json.loads(row["error"])}
            return None
        now = time.time()
        self.manager.store.execute(
            """INSERT INTO requests(request_id,action,payload_hash,state,created_at,updated_at)
               VALUES (?,?,?,'PENDING',?,?)""",
            (request_id, action, digest, now, now),
        )
        return None

    def _finish_request(self, request_id, response):
        now = time.time()
        if response.get("ok"):
            self.manager.store.execute(
                "UPDATE requests SET state='DONE',result_json=?,updated_at=? WHERE request_id=?",
                (json.dumps(response, ensure_ascii=False), now, request_id),
            )
        else:
            self.manager.store.execute(
                "UPDATE requests SET state='FAILED',error=?,updated_at=? WHERE request_id=?",
                (json.dumps(response["error"], ensure_ascii=False), now, request_id),
            )

    def process(self, request):
        if request.get("protocol_version") != PROTOCOL_VERSION:
            return self._response_error("INVALID_REQUEST", "不支持的协议版本")
        request_id = request.get("request_id")
        action = request.get("action")
        payload = request.get("payload", {})
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            return self._response_error("INVALID_REQUEST", "request_id 无效")
        if not isinstance(action, str) or not isinstance(payload, dict):
            return self._response_error("INVALID_REQUEST", "action 或 payload 无效")
        try:
            remembered = self._remembered_request(request_id, action, payload)
            if remembered is not None:
                return remembered
            response = {"ok": True, "data": self.action(action, payload, request_id)}
        except ValueError as exc:
            response = self._response_error("REJECTED", str(exc))
        except (OSError, TimeoutError) as exc:
            response = self._response_error("UNAVAILABLE", str(exc))
        except Exception as exc:
            response = self._response_error("INTERNAL", f"{type(exc).__name__}: {exc}")
        self._finish_request(request_id, response)
        return response

    def _snapshot(self):
        value = self.manager.snapshot()
        for account in value.get("accounts", []):
            account["has_qr"] = bool(account.pop("qr_bytes", b""))
        return value

    def action(self, action, payload, request_id):
        if action == "doctor":
            self.manager.runtime.connect()
            return {
                "service": "running",
                "docker": "connected",
                "managed_containers": len(self.manager.runtime.list_owned()),
                "data_dir": str(self.paths.data_dir),
                "runtime_dir": str(self.paths.runtime_dir),
            }
        if action == "status":
            return self._snapshot()
        if action == "account_list":
            return [{k: row[k] for k in ("id", "display_name", "enabled", "auth_state", "blocked_reason")} for row in self.manager.snapshot()["accounts"]]
        if action == "account_add":
            return {"id": self.manager.add_account(payload["name"])}
        if action == "account_edit":
            account = self.manager.account(payload["id"])
            schedule = self.manager.store.one("SELECT * FROM schedules WHERE account_id=?", (payload["id"],))
            self.manager.edit_account(
                payload["id"],
                payload.get("name") or account["display_name"],
                account["enabled"] if payload.get("enabled") is None else payload["enabled"],
                schedule["local_time"],
                schedule["enabled"],
                account["timeout_seconds"] if payload.get("timeout") is None else payload["timeout"],
                payload.get("patch") or {},
            )
            return {"id": payload["id"]}
        if action == "image_prepare":
            return self.start_image_job()
        if action == "job_status":
            row = self.manager.store.one("SELECT * FROM jobs WHERE id=?", (payload["id"],))
            if not row:
                raise ValueError("作业不存在")
            if row["result_json"]:
                row["result"] = json.loads(row.pop("result_json"))
            else:
                row.pop("result_json", None)
            return row
        if action == "run":
            return {"trigger_id": self.manager.enqueue(payload["id"], payload.get("task", "main"), request_id)}
        if action == "stop":
            if payload.get("all"):
                self.manager.cancel_all()
                return {"stopped": "all"}
            self.manager.cancel(payload["id"])
            return {"stopped": payload["id"]}
        if action == "schedule_set":
            self.manager.set_schedule(payload["id"], payload["time"], payload["enabled"])
            return {"id": payload["id"], "time": payload["time"], "enabled": payload["enabled"]}
        if action == "schedule_list":
            return [
                {"id": a["id"], "display_name": a["display_name"], "schedule": a["schedule"]}
                for a in self.manager.snapshot()["accounts"]
            ]
        if action == "concurrency_set":
            self.manager.set_concurrency(payload["value"])
            return {"concurrency": payload["value"]}
        if action == "logs":
            account = self.manager.account(payload["id"])
            run = self.manager.store.one(
                "SELECT * FROM runs WHERE account_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
                (account["id"],),
            )
            if not run:
                return {"account": account["id"], "run": None, "tail": ""}
            chunks = []
            for name in ("stdout.log", "stderr.log"):
                try:
                    file = self.manager.root / "runs" / run["id"] / name
                    with file.open("rb") as stream:
                        stream.seek(0, 2)
                        stream.seek(max(0, stream.tell() - 40000))
                        chunks.append(stream.read().decode("utf-8", "replace"))
                except OSError:
                    pass
            return {"account": account["id"], "run_id": run["id"], "state": run["state"], "tail": "\n".join(chunks)}
        if action == "history":
            sql = """SELECT r.*,a.display_name FROM runs r JOIN accounts a ON a.id=r.account_id"""
            params = []
            if payload.get("account"):
                sql += " WHERE r.account_id=?"
                params.append(payload["account"])
            sql += " ORDER BY r.created_at DESC,r.rowid DESC LIMIT ?"
            params.append(max(1, min(int(payload.get("limit", 20)), 100)))
            return self.manager.store.rows(sql, params)
        if action == "login_qr":
            account = next((a for a in self.manager.snapshot()["accounts"] if a["id"] == payload["id"]), None)
            if not account or not account.get("qr_bytes"):
                raise ValueError("当前没有本次等待扫码的有效二维码")
            return {"content": base64.b64encode(account["qr_bytes"]).decode("ascii")}
        if action == "diagnostics_export":
            path = Path(self.manager.export_diagnostics())
            return json.loads(path.read_text(encoding="utf-8"))
        raise ValueError(f"未知 action：{action}")

    def start_image_job(self):
        if self.image_future and not self.image_future.done():
            return {"job_id": self.image_job_id, "accepted": True}
        if self.manager.store.active():
            raise ValueError("请等待当前任务结束后准备镜像")
        job_id = uuid.uuid4().hex
        now = time.time()
        self.manager.store.execute(
            "INSERT INTO jobs(id,kind,state,created_at,updated_at) VALUES (?,?,'RUNNING',?,?)",
            (job_id, "image_prepare", now, now),
        )
        pinned = self.manager.store.setting("image_digest", "")
        installation = self.manager.store.setting("installation_id")
        self.image_job_id = job_id
        self.manager.set_dispatch_paused(True)
        self.image_future = self.jobs.submit(self._prepare_image_worker, installation, pinned)
        return {"job_id": job_id, "accepted": True}

    @staticmethod
    def _prepare_image_worker(installation, pinned):
        runtime = DockerRuntime(installation)
        try:
            return runtime.prepare_image(pinned)
        finally:
            runtime.close()

    def poll_image_job(self):
        if not self.image_future or not self.image_future.done():
            return
        job_id, future = self.image_job_id, self.image_future
        self.image_future = None
        self.image_job_id = None
        try:
            digest = future.result()
            self.manager.set_image(digest)
            self.manager.store.execute(
                "UPDATE jobs SET state='DONE',progress=1,message='镜像已固定',result_json=?,updated_at=? WHERE id=?",
                (json.dumps({"digest": digest}, ensure_ascii=False), time.time(), job_id),
            )
        except Exception as exc:
            self.manager.store.execute(
                "UPDATE jobs SET state='FAILED',error=?,message='镜像准备失败',updated_at=? WHERE id=?",
                (str(exc), time.time(), job_id),
            )
        finally:
            self.manager.set_dispatch_paused(False)

    def serve(self):
        self.paths.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.server.start()
        try:
            while not self.stop_event.is_set():
                self.poll_image_job()
                for _ in range(20):
                    pending = self.server.get()
                    if not pending:
                        break
                    self.server.reply(pending, self.process(pending.request))
                self.manager.tick()
                self.stop_event.wait(2.5)
        finally:
            self.server.close()
            self.manager.set_dispatch_paused(True)
            self.jobs.shutdown(wait=False, cancel_futures=True)
            self.manager.close()
            self.lock.release()

    def stop(self, *_):
        self.manager.shutdown(cancel_tasks=False)
        self.stop_event.set()


def run(paths: AppPaths):
    try:
        daemon = Daemon(paths)
    except RuntimeError as exc:
        print(str(exc), file=__import__("sys").stderr)
        return 3
    signal.signal(signal.SIGTERM, daemon.stop)
    signal.signal(signal.SIGINT, daemon.stop)
    try:
        daemon.serve()
    except RuntimeError as exc:
        print(str(exc), file=__import__("sys").stderr)
        daemon.manager.close()
        daemon.lock.release()
        return 3
    return 0


__all__ = ["run"]
