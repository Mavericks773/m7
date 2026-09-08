from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import docker
from docker.errors import NotFound
from docker.types import LogConfig, Mount

REPOSITORY = "ghcr.io/moesnow/march7thassistant"
DIGEST = re.compile(r"^ghcr\.io/moesnow/march7thassistant@sha256:[0-9a-f]{64}$")
OWNER = "io.m7manager.installation"
ACCOUNT = "io.m7manager.account"
RUN = "io.m7manager.run"


@dataclass
class ContainerSnapshot:
    id: str
    labels: dict
    status: str
    started_at: float | None = None
    exit_code: int | None = None
    oom_killed: bool = False


def docker_timestamp(text):
    if not text or text.startswith("0001-"):
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


class DockerRuntime:
    """Local endpoints only. Never follows a remote DOCKER_HOST or active CLI context."""

    def __init__(self, installation_id: str):
        self.installation_id = installation_id
        self.client = None

    def connect(self):
        if self.client:
            try:
                self.client.ping()
                return
            except Exception:
                self.client.close()
                self.client = None
        endpoints = (
            ["npipe:////./pipe/dockerDesktopLinuxEngine", "npipe:////./pipe/docker_engine"]
            if os.name == "nt"
            else ["unix:///var/run/docker.sock"]
        )
        for endpoint in endpoints:
            client = None
            try:
                client = docker.DockerClient(base_url=endpoint, timeout=5)
                if client.info().get("OSType") != "linux":
                    raise RuntimeError("请切换至 Linux 容器模式")
                self.client = client
                return
            except Exception:
                if client:
                    client.close()
        raise RuntimeError(
            "无法连接本地 Linux Docker。请启动 Docker Desktop 并确认 Linux 容器引擎就绪。"
        )

    def name(self, account_id):
        return f"m7mgr-{self.installation_id}-{account_id}"

    def _owned(self, container_id, account_id=None, run_id=None):
        container = self.client.containers.get(container_id)
        labels = container.labels
        if labels.get(OWNER) != self.installation_id:
            raise ValueError("拒绝操作不属于本管理器的容器")
        if account_id and labels.get(ACCOUNT) != account_id:
            raise ValueError("容器账号标签不匹配")
        if run_id and labels.get(RUN) != run_id:
            raise ValueError("容器运行标签不匹配")
        return container

    def list_owned(self):
        containers = self.client.containers.list(
            all=True, filters={"label": f"{OWNER}={self.installation_id}"}
        )
        return [self._snapshot(c) for c in containers]

    @staticmethod
    def _snapshot(container):
        state = container.attrs["State"]
        return ContainerSnapshot(
            container.id,
            container.labels,
            state["Status"],
            docker_timestamp(state.get("StartedAt")),
            state.get("ExitCode"),
            state.get("OOMKilled", False),
        )

    def inspect(self, container_id, account_id, run_id):
        return self._snapshot(self._owned(container_id, account_id, run_id))

    def ensure_image(self, image):
        if not DIGEST.fullmatch(image):
            raise ValueError("请先准备官方镜像，并使用固定的 sha256 digest")
        self.client.images.get(image)  # Never pull or silently upgrade while dispatching.

    def prepare_image(self, pinned=""):
        self.connect()
        if pinned:
            if not DIGEST.fullmatch(pinned):
                raise ValueError("已保存的镜像 digest 无效")
            try:
                self.ensure_image(pinned)
                return pinned
            except NotFound:
                pass
        # Pulling the multi-GB image has a different timeout from ordinary inspection.
        self.client.api.timeout = 600
        try:
            image = (
                self.client.images.pull(pinned)
                if pinned
                else self.client.images.pull(REPOSITORY, tag="latest")
            )
        finally:
            self.client.api.timeout = 5
        matches = [d for d in image.attrs.get("RepoDigests", []) if DIGEST.fullmatch(d)]
        if not matches:
            raise ValueError("镜像缺少可固定的官方 RepoDigest")
        if pinned and pinned not in matches:
            raise ValueError("镜像下载结果与固定 digest 不一致")
        return pinned or matches[0]

    def create(self, run, path: Path):
        self.ensure_image(run["image_digest"])
        destinations = {
            "config.yaml": "/m7a/config.yaml",
            "logs": "/m7a/logs",
            "browser-profile": "/m7a/3rdparty/WebBrowser/UserProfile",
        }
        mounts = []
        for source, destination in destinations.items():
            host = (path / source).resolve()
            if not host.is_relative_to(path.resolve()):
                raise ValueError("挂载路径越出账号目录")
            if (source == "config.yaml" and not host.is_file()) or not host.exists():
                raise ValueError("缺少有效的账号配置或数据目录")
            mounts.append(Mount(destination, str(host), type="bind", read_only=False))
        container = self.client.containers.create(
            run["image_digest"],
            ["python", "main.py", run["task"]],
            name=self.name(run["account_id"]),
            mounts=mounts,
            labels={OWNER: self.installation_id, ACCOUNT: run["account_id"], RUN: run["id"]},
            environment={
                "TZ": "Asia/Shanghai",
                "MARCH7TH_CLOUD_GAME_ENABLE": "true",
                "MARCH7TH_BROWSER_HEADLESS_ENABLE": "true",
                "MARCH7TH_BROWSER_HEADLESS_RESTART_ON_NOT_LOGGED_IN": "false",
                "MARCH7TH_AFTER_FINISH": "Exit",
                "MARCH7TH_DOCKER_STARTED": "true",
                "MARCH7TH_LOG_LEVEL": "INFO",
            },
            restart_policy={"Name": "no"},
            shm_size="1g",
            network_mode="bridge",
            init=True,
            tty=False,
            log_config=LogConfig(type="json-file", config={"max-size": "10m", "max-file": "3"}),
        )
        return container.id

    def start(self, container_id, account_id, run_id):
        self._owned(container_id, account_id, run_id).start()

    def stop(self, container_id, account_id, run_id):
        container = self._owned(container_id, account_id, run_id)
        self.client.api.timeout = 30
        try:
            container.stop(timeout=20)
        finally:
            self.client.api.timeout = 5

    def remove(self, container_id, account_id, run_id):
        container = self._owned(container_id, account_id, run_id)
        if container.status not in ("exited", "created", "dead"):
            raise ValueError("运行中容器不能移除")
        container.remove()  # Never force-remove, never remove volumes.

    def logs(self, container_id, account_id, run_id, final=False):
        container = self._owned(container_id, account_id, run_id)
        limit = 10000 if final else 2000
        # Bounded retained tail, not an unbounded in-memory live log stream.
        return tuple(
            container.logs(stdout=stdout, stderr=not stdout, timestamps=True, tail=limit).decode(
                "utf-8", errors="replace"
            )[-2_000_000:]
            for stdout in (True, False)
        )

    def close(self):
        if self.client:
            self.client.close()


__all__ = ["DockerRuntime", "ContainerSnapshot", "NotFound"]
