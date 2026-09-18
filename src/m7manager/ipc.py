from __future__ import annotations

import json
import os
import queue
import socket
import stat
import threading
from dataclasses import dataclass
from pathlib import Path

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def _frame(value, limit):
    payload = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(payload) > limit:
        raise ValueError("IPC 消息超过大小限制")
    return payload


def send_request(socket_path: Path, request: dict, timeout: float = 10) -> dict:
    if not hasattr(socket, "AF_UNIX"):
        raise OSError("当前平台不支持 Unix socket")
    payload = _frame(request, MAX_REQUEST_BYTES)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(socket_path))
        client.sendall(payload)
        data = bytearray()
        while len(data) <= MAX_RESPONSE_BYTES:
            chunk = client.recv(min(65536, MAX_RESPONSE_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if b"\n" in chunk:
                break
        if len(data) > MAX_RESPONSE_BYTES:
            raise ValueError("IPC 响应超过大小限制")
        line = bytes(data).split(b"\n", 1)[0]
        if not line:
            raise RuntimeError("服务返回空响应")
        return json.loads(line.decode("utf-8"))
    finally:
        client.close()


@dataclass
class PendingRequest:
    request: dict
    event: threading.Event
    response: dict | None = None


class IPCServer:
    def __init__(self, socket_path: Path):
        self.socket_path = Path(socket_path)
        self.pending: queue.Queue[PendingRequest] = queue.Queue()
        self._server = None
        self._thread = None
        self._closed = threading.Event()

    def start(self):
        if not hasattr(socket, "AF_UNIX"):
            raise RuntimeError("daemon 需要支持 Unix socket 的系统")
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.socket_path.parent, 0o700)
        if self.socket_path.exists():
            if not stat.S_ISSOCK(self.socket_path.stat().st_mode):
                raise RuntimeError(f"运行时路径不是 Unix socket：{self.socket_path}")
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(0.2)
            try:
                probe.connect(str(self.socket_path))
            except OSError:
                self.socket_path.unlink()
            else:
                raise RuntimeError(f"控制 socket 已在使用：{self.socket_path}")
            finally:
                probe.close()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        self._server.listen(32)
        self._server.settimeout(0.5)
        self._thread = threading.Thread(target=self._accept_loop, name="m7-ipc", daemon=True)
        self._thread.start()

    def _accept_loop(self):
        while not self._closed.is_set():
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._connection, args=(conn,), daemon=True).start()

    def _connection(self, conn):
        conn.settimeout(10)
        try:
            data = bytearray()
            while len(data) <= MAX_REQUEST_BYTES:
                chunk = conn.recv(min(65536, MAX_REQUEST_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if b"\n" in chunk:
                    break
            if len(data) > MAX_REQUEST_BYTES:
                raise ValueError("IPC 请求超过大小限制")
            line = bytes(data).split(b"\n", 1)[0]
            request = json.loads(line.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("IPC 请求必须是 JSON 对象")
            pending = PendingRequest(request, threading.Event())
            self.pending.put(pending)
            if not pending.event.wait(300):
                response = {"ok": False, "error": {"code": "TIMEOUT", "message": "服务处理超时"}}
            else:
                response = pending.response or {"ok": False, "error": {"code": "INTERNAL", "message": "服务未返回结果"}}
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            response = {"ok": False, "error": {"code": "INVALID_REQUEST", "message": str(exc)}}
        try:
            conn.sendall(_frame(response, MAX_RESPONSE_BYTES))
        except OSError:
            pass
        finally:
            conn.close()

    def get(self, timeout=0):
        try:
            return self.pending.get(timeout=timeout)
        except queue.Empty:
            return None

    @staticmethod
    def reply(pending: PendingRequest, response: dict):
        pending.response = response
        pending.event.set()

    def close(self):
        self._closed.set()
        if self._server:
            self._server.close()
        if self._thread:
            self._thread.join(timeout=1)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass


__all__ = ["IPCServer", "PROTOCOL_VERSION", "send_request"]
