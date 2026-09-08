"""Opt-in lifecycle test. Pre-pull python:3.14-alpine; never logs into the game."""

import os
import uuid
from pathlib import Path

import pytest
from docker.types import Mount

from m7manager.docker_runtime import ACCOUNT, OWNER, RUN, DockerRuntime


@pytest.mark.docker
@pytest.mark.skipif(
    os.environ.get("M7_TEST_DOCKER") != "1", reason="set M7_TEST_DOCKER=1 for real Docker"
)
def test_real_local_bind_mount_and_owned_lifecycle(tmp_path):
    runtime = DockerRuntime("integration-" + uuid.uuid4().hex[:8])
    runtime.connect()
    image = "python:3.14-alpine"
    runtime.client.images.get(image)
    cid = None
    try:
        folder = tmp_path / "中文 路径"
        folder.mkdir()
        container = runtime.client.containers.create(
            image,
            [
                "python",
                "-c",
                "from pathlib import Path; Path('/data/probe.txt').write_text('ok'); print('finished')",
            ],
            labels={OWNER: runtime.installation_id, ACCOUNT: "probe", RUN: "test"},
            mounts=[Mount("/data", str(folder.resolve()), type="bind")],
        )
        cid = container.id
        runtime.start(cid, "probe", "test")
        container.wait(timeout=30)
        snap = runtime.inspect(cid, "probe", "test")
        assert snap.exit_code == 0 and snap.status == "exited"
        assert Path(folder / "probe.txt").read_text() == "ok"
        assert "finished" in runtime.logs(cid, "probe", "test", final=True)[0]
    finally:
        if cid:
            container.reload()
            if container.status == "running":
                runtime.stop(cid, "probe", "test")
            runtime.remove(cid, "probe", "test")
        runtime.close()
