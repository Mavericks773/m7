from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path

from .docker_runtime import DockerRuntime
from .ipc import PROTOCOL_VERSION, send_request
from .locking import DataDirLock
from .paths import AppPaths, resolve_paths

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_ARGUMENT = 2
EXIT_UNAVAILABLE = 3
EXIT_REJECTED = 4
EXIT_TIMEOUT = 5


def build_parser():
    parser = argparse.ArgumentParser(description="M7 双账号 Docker 管理器")
    parser.add_argument("--data-dir", type=Path, help="持久数据目录")
    parser.add_argument("--runtime-dir", type=Path, help="Unix socket 运行时目录")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    parser.add_argument("--doctor", action="store_true", help="兼容别名：执行 doctor")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("daemon", help="前台运行后台服务")
    sub.add_parser("gui", help="启动桌面界面")
    sub.add_parser("doctor", help="检查服务、数据目录和本地 Docker")
    for name in ("status", "schedule-list"):
        sub.add_parser(name)

    account = sub.add_parser("account", help="管理账号")
    account_sub = account.add_subparsers(dest="account_command", required=True)
    add = account_sub.add_parser("add")
    add.add_argument("name")
    account_sub.add_parser("list")
    edit = account_sub.add_parser("edit")
    edit.add_argument("id")
    edit.add_argument("--name")
    enabled = edit.add_mutually_exclusive_group()
    enabled.add_argument("--enable", action="store_true")
    enabled.add_argument("--disable", action="store_true")
    edit.add_argument("--timeout", type=int)
    edit.add_argument("--patch", default="{}", help="允许编辑的 YAML/JSON 字段 JSON")

    image = sub.add_parser("image", help="管理固定官方镜像")
    image_sub = image.add_subparsers(dest="image_command", required=True)
    image_sub.add_parser("prepare")
    job = sub.add_parser("job", help="查询后台作业")
    job_sub = job.add_subparsers(dest="job_command", required=True)
    job_status = job_sub.add_parser("status")
    job_status.add_argument("id")

    run = sub.add_parser("run")
    run.add_argument("id")
    run.add_argument("--task", choices=("main", "daily", "power"), default="main")
    stop = sub.add_parser("stop")
    stop.add_argument("id", nargs="?")
    stop.add_argument("--all", action="store_true")

    schedule = sub.add_parser("schedule")
    schedule_sub = schedule.add_subparsers(dest="schedule_command", required=True)
    schedule_set = schedule_sub.add_parser("set")
    schedule_set.add_argument("id")
    schedule_set.add_argument("--time", required=True)
    schedule_set.add_argument("--enabled", choices=("true", "false"), required=True)
    schedule_sub.add_parser("list")

    concurrency = sub.add_parser("concurrency")
    concurrency_sub = concurrency.add_subparsers(dest="concurrency_command", required=True)
    concurrency_set = concurrency_sub.add_parser("set")
    concurrency_set.add_argument("value", type=int, choices=(1, 2))

    logs = sub.add_parser("logs")
    logs.add_argument("id")
    logs.add_argument("--follow", action="store_true")
    history = sub.add_parser("history")
    history.add_argument("--account")
    history.add_argument("--limit", type=int, default=20)
    login = sub.add_parser("login")
    login_sub = login.add_subparsers(dest="login_command", required=True)
    qr = login_sub.add_parser("qr")
    qr.add_argument("id")
    qr.add_argument("--output", type=Path, required=True)
    diagnostics = sub.add_parser("diagnostics")
    diagnostics_sub = diagnostics.add_subparsers(dest="diagnostics_command", required=True)
    diagnostics_export = diagnostics_sub.add_parser("export")
    diagnostics_export.add_argument("--output", type=Path, required=True)
    return parser


def _request(paths: AppPaths, action: str, payload: dict, timeout=10):
    return send_request(
        paths.runtime_dir / "control.sock",
        {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": uuid.uuid4().hex,
            "action": action,
            "payload": payload,
        },
        timeout=timeout,
    )


def _print(value, as_json):
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                print(f"{key}: {json.dumps(item, ensure_ascii=False)}")
            else:
                print(f"{key}: {item}")
    else:
        print(value)


def _exit_for_error(error):
    code = (error or {}).get("code", "INTERNAL")
    return {
        "INVALID_ARGUMENT": EXIT_ARGUMENT,
        "INVALID_REQUEST": EXIT_ARGUMENT,
        "UNAVAILABLE": EXIT_UNAVAILABLE,
        "REJECTED": EXIT_REJECTED,
        "TIMEOUT": EXIT_TIMEOUT,
    }.get(code, EXIT_INTERNAL)


def _offline_doctor(paths: AppPaths, as_json):
    result = {
        "service": "stopped",
        "data_dir": str(paths.data_dir),
        "runtime_dir": str(paths.runtime_dir),
        "data_dir_exists": paths.data_dir.exists(),
    }
    lock = None
    if paths.data_dir.exists():
        lock = DataDirLock(paths.data_dir)
        result["data_dir_in_use"] = not lock.acquire()
        if not result["data_dir_in_use"]:
            lock.release()
    else:
        result["data_dir_in_use"] = False
    try:
        runtime = DockerRuntime("doctor")
        runtime.connect()
        result["docker"] = "connected"
        result["docker_ostype"] = "linux"
        result["managed_containers"] = len(runtime.list_owned())
        runtime.close()
    except Exception as exc:
        result["docker"] = "unavailable"
        result["docker_reason"] = str(exc)
    db = paths.data_dir / "manager.db"
    if db.exists():
        try:
            connection = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
            row = connection.execute("SELECT value FROM settings WHERE key='image_digest'").fetchone()
            result["image_digest"] = json.loads(row[0]) if row else ""
            connection.close()
        except sqlite3.Error as exc:
            result["database"] = f"unreadable: {exc}"
    _print(result, as_json)
    return EXIT_OK if result.get("docker") == "connected" and not result["data_dir_in_use"] else EXIT_INTERNAL


def _run_gui(paths: AppPaths):
    # This is the only path that imports PySide6.
    from PySide6.QtWidgets import QApplication, QMessageBox

    from .services import Manager
    from .ui import MainWindow

    app = QApplication([])
    app.setApplicationName("M7AccountManager")
    lock = DataDirLock(paths.data_dir)
    if not lock.acquire():
        QMessageBox.warning(None, "M7 已在运行", "此数据目录已有管理器运行，请查看系统托盘。")
        return 1
    manager = Manager(paths.data_dir)
    try:
        app.setQuitOnLastWindowClosed(False)
        window = MainWindow(manager)
        window.show()
        code = app.exec()
        if window.thread:
            window.thread.quit()
            window.thread.wait()
        return code
    finally:
        manager.close()
        lock.release()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args()
    command = "doctor" if args.doctor else args.command
    paths = resolve_paths(args.data_dir, args.runtime_dir)
    if command is None:
        if os.name == "nt":
            return _run_gui(paths)
        parser.print_help()
        return EXIT_OK
    if command == "gui":
        return _run_gui(paths)
    if command == "daemon":
        from .daemon import run

        return run(paths)
    if command == "doctor":
        try:
            response = _request(paths, "doctor", {}, timeout=3)
            if response.get("ok"):
                _print(response["data"], args.json)
                return EXIT_OK
        except (OSError, TimeoutError, ValueError):
            pass
        return _offline_doctor(paths, args.json)

    payload = {}
    action = command.replace("-", "_")
    if command == "status":
        action = "status"
    elif command == "schedule-list":
        action = "schedule_list"
    elif command == "account":
        action = f"account_{args.account_command}"
        if args.account_command == "add":
            payload = {"name": args.name}
        elif args.account_command == "edit":
            try:
                patch = json.loads(args.patch)
            except json.JSONDecodeError as exc:
                parser.error(f"--patch 不是有效 JSON：{exc}")
            payload = {
                "id": args.id,
                "name": args.name,
                "enabled": True if args.enable else False if args.disable else None,
                "timeout": args.timeout,
                "patch": patch,
            }
    elif command == "image":
        action = "image_prepare"
    elif command == "job":
        action = "job_status"
        payload = {"id": args.id}
    elif command == "run":
        payload = {"id": args.id, "task": args.task}
    elif command == "stop":
        if not args.all and not args.id:
            parser.error("stop 需要账号 ID，或使用 --all")
        payload = {"id": args.id, "all": args.all}
    elif command == "schedule":
        action = f"schedule_{args.schedule_command}"
        if args.schedule_command == "set":
            payload = {"id": args.id, "time": args.time, "enabled": args.enabled == "true"}
    elif command == "concurrency":
        action = "concurrency_set"
        payload = {"value": args.value}
    elif command == "logs":
        payload = {"id": args.id, "follow": args.follow}
    elif command == "history":
        payload = {"account": args.account, "limit": max(1, min(args.limit, 100))}
    elif command == "login":
        if args.login_command != "qr":
            parser.error("不支持的 login 子命令")
        action = "login_qr"
        payload = {"id": args.id}
    elif command == "diagnostics":
        action = "diagnostics_export"

    try:
        response = _request(paths, action, payload, timeout=15 if action != "logs" else 10)
    except (OSError, TimeoutError) as exc:
        print(f"服务不可用：{exc}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ARGUMENT
    if not response.get("ok"):
        error = response.get("error", {})
        print(error.get("message", "操作失败"), file=sys.stderr)
        return _exit_for_error(error)
    data = response.get("data", {})
    if command == "login":
        output = args.output.expanduser()
        if output.exists():
            print(f"输出文件已存在，不覆盖：{output}", file=sys.stderr)
            return EXIT_ARGUMENT
        import base64

        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(base64.b64decode(data["content"]))
        os.chmod(output, 0o600)
        _print({"output": str(output), "bytes": output.stat().st_size}, args.json)
    elif command == "diagnostics":
        output = args.output.expanduser()
        if output.exists():
            print(f"输出文件已存在，不覆盖：{output}", file=sys.stderr)
            return EXIT_ARGUMENT
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(output, 0o600)
        _print({"output": str(output)}, args.json)
    else:
        _print(data, args.json)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
