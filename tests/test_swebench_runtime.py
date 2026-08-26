from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from evals import swebench_runtime


class _Container:
    name = "runtime"

    def __init__(self):
        self.started = False
        self.removed = False

    def start(self):
        self.started = True

    def exec_run(self, command):
        assert "eval.sh" in command[-1]
        return SimpleNamespace(exit_code=0)

    def remove(self, force=False):
        assert force is True
        self.removed = True


class _Containers:
    def __init__(self):
        self.kwargs = None
        self.container = _Container()

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.container


class _Client:
    def __init__(self):
        self.containers = _Containers()
        self.closed = False

    def close(self):
        self.closed = True


def test_official_runtime_mounts_only_testbed_and_hides_grader(
    tmp_path: Path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "module.py").write_text("VALUE = 1\n")
    client = _Client()
    spec = SimpleNamespace(instance_id="owner__repo-1", platform="linux/x86_64")
    monkeypatch.setattr(
        swebench_runtime,
        "prepare_official_source",
        lambda *_args, **_kwargs: (source, "official-image", "sha256:abc"),
    )
    monkeypatch.setattr(
        swebench_runtime,
        "_docker_and_spec",
        lambda *_args, **_kwargs: (client, spec),
    )

    runtime = swebench_runtime.create_official_runtime(
        {"instance_id": "owner__repo-1"}, tmp_path / "prepared", "react"
    )

    create_args = client.containers.kwargs
    assert list(create_args["volumes"].values()) == [{"bind": "/testbed", "mode": "rw"}]
    assert all("eval" not in path for path in create_args["volumes"])
    assert create_args["working_dir"] == "/testbed"
    assert (runtime.host_worktree / "module.py").exists()
    assert runtime.tool_runtime.command_prefix[:4] == (
        "docker",
        "exec",
        "-w",
        "/testbed",
    )

    runtime.close()
    assert client.containers.container.removed is True
    assert client.closed is True
