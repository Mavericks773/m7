from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="M7 本地双账号 Docker 管理器")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("LOCALAPPDATA", Path.home())) / "M7AccountManager",
    )
    parser.add_argument("--doctor", action="store_true", help="检查本地 Docker，不启动游戏")
    args = parser.parse_args()
    from PySide6.QtCore import QLockFile
    from PySide6.QtWidgets import QApplication, QMessageBox

    from .services import Manager

    args.data_dir.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    app.setApplicationName("M7AccountManager")
    lock = QLockFile(str(args.data_dir.resolve() / "manager.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        if args.doctor:
            print("数据目录已由另一个管理器使用")
        else:
            QMessageBox.warning(None, "M7 已在运行", "此数据目录已有管理器运行，请查看系统托盘。")
        return 1
    manager = Manager(args.data_dir)
    if args.doctor:
        try:
            manager.runtime.connect()
            print(
                json.dumps(
                    {
                        "docker": "connected",
                        "os": "linux",
                        "managed_containers": len(manager.runtime.list_owned()),
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        except Exception as exc:
            print(json.dumps({"docker": "unavailable", "reason": str(exc)}, ensure_ascii=False))
            return 1
        finally:
            manager.close()
            lock.unlock()
    from .ui import MainWindow

    app.setQuitOnLastWindowClosed(False)
    window = MainWindow(manager)
    window.show()
    code = app.exec()
    if window.thread:
        window.thread.quit()
        window.thread.wait()
    lock.unlock()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
