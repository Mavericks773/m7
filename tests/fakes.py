import copy

from m7manager.docker_runtime import ACCOUNT, OWNER, RUN, ContainerSnapshot

IMAGE = "ghcr.io/moesnow/march7thassistant@sha256:" + "a" * 64


class FakeRuntime:
    def __init__(self, clock):
        self.clock = clock
        self.containers = {}
        self.output = {}
        self.calls = []
        self.available = True
        self.fail_after_create = False
        self.fail_after_start = False

    def connect(self):
        if not self.available:
            raise ConnectionError("Docker disconnected")

    def ensure_image(self, image):
        assert image == IMAGE

    def prepare_image(self, pinned=""):
        return IMAGE

    def list_owned(self):
        self.connect()
        return copy.deepcopy(list(self.containers.values()))

    def create(self, run, path):
        assert not any(c.labels[ACCOUNT] == run["account_id"] for c in self.containers.values())
        cid = "container-" + run["id"]
        self.containers[cid] = ContainerSnapshot(
            cid, {OWNER: "test", ACCOUNT: run["account_id"], RUN: run["id"]}, "created"
        )
        self.output[cid] = ("", "")
        self.calls.append(("create", cid, path))
        if self.fail_after_create:
            self.fail_after_create = False
            raise ConnectionError("Response lost after create")
        return cid

    def start(self, cid, account, run):
        c = self.containers[cid]
        assert c.labels[ACCOUNT] == account and c.labels[RUN] == run
        c.status = "running"
        c.started_at = self.clock()
        self.calls.append(("start", cid))
        if self.fail_after_start:
            self.fail_after_start = False
            raise ConnectionError("Response lost after start")

    def stop(self, cid, account, run):
        self.calls.append(("stop", cid))
        self.finish(cid, 137)

    def remove(self, cid, account, run):
        assert self.containers[cid].status in ("exited", "created", "dead")
        del self.containers[cid]
        self.calls.append(("remove", cid))

    def logs(self, cid, account, run, final=False):
        return self.output[cid]

    def finish(self, cid, code=0, oom=False):
        self.containers[cid].status = "exited"
        self.containers[cid].exit_code = code
        self.containers[cid].oom_killed = oom

    def close(self):
        pass
