"""Official SWE-bench image runtime used while an agent works on a task.

The test patch and evaluator script are never copied into the agent container.
Only ``/testbed`` from the official instance image is extracted, bind-mounted,
and exposed to Xenon's tools.  Final grading remains the official harness's
responsibility.
"""

from __future__ import annotations

import logging
import os
import shutil
import tarfile
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xenon.engine.tool_runtime import ToolRuntime

logger = logging.getLogger(__name__)


def _docker_and_spec(instance: dict[str, Any], namespace: str | None = "swebench"):
    try:
        import docker
        from swebench.harness.test_spec.test_spec import make_test_spec
    except ImportError as exc:  # pragma: no cover - depends on benchmark venv
        raise RuntimeError(
            "official SWE-bench runtime requires the docker and swebench packages"
        ) from exc
    return docker.from_env(), make_test_spec(instance, namespace=namespace)


def _apply_test_patch(worktree: Path, test_patch: str, instance_id: str) -> None:
    """Apply the official FAIL_TO_PASS test patch into an engine worktree.

    - Applies with ``git apply`` from the repo root (test paths are repo-relative).
    - Commits immediately so the final ``git diff`` reflects only the agent's
      edits, not the injected test patch.
    - **Anti-cheat**: after applying, the touched test files are made read-only
      on disk. The agent therefore cannot edit them to make FAIL_TO_PASS pass
      by tampering with the tests themselves; any write attempt fails loudly.
    """
    import subprocess

    patch_file = worktree / ".test_patch"
    patch_file.write_text(test_patch, encoding="utf-8")
    try:
        apply = subprocess.run(
            ["git", "-C", str(worktree), "apply", "--whitespace=fix", str(patch_file)],
            capture_output=True, text=True, timeout=60,
        )
        if apply.returncode != 0:
            # Fallback: try patch -p1 (some test patches use context that git
            # apply rejects but GNU patch accepts).
            fallback = subprocess.run(
                ["patch", "-p1", "-d", str(worktree), "-i", str(patch_file)],
                capture_output=True, text=True, timeout=60,
            )
            if fallback.returncode != 0:
                raise RuntimeError(
                    f"test_patch application failed for {instance_id}: "
                    f"{apply.stderr[:300]} / {fallback.stderr[:300]}"
                )
    finally:
        patch_file.unlink(missing_ok=True)

    # Anti-cheat: lock the touched test files read-only.
    touched = subprocess.run(
        ["git", "-C", str(worktree), "diff", "--name-only", "HEAD"],
        capture_output=True, text=True, timeout=30,
    )
    for rel in touched.stdout.splitlines():
        path = worktree / rel
        if path.is_file():
            path.chmod(0o444)

    subprocess.run(
        ["git", "-C", str(worktree), "add", "-A"],
        capture_output=True, text=True, timeout=60,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "-q", "-m", "apply test_patch (eval)"],
        capture_output=True, text=True, timeout=60,
    )


def _extract_testbed(container: Any, destination: Path) -> None:
    stream, _ = container.get_archive("/testbed")
    destination.mkdir(parents=True, exist_ok=False)
    with tempfile.NamedTemporaryFile(suffix=".tar") as archive:
        for chunk in stream:
            archive.write(chunk)
        archive.flush()
        with tarfile.open(archive.name) as bundle:
            # Python's data filter rejects device files, absolute paths and
            # traversal entries supplied by a compromised image archive.
            bundle.extractall(destination, filter="data")


def prepare_official_source(
    instance: dict[str, Any],
    prepared_root: Path,
    *,
    namespace: str | None = "swebench",
) -> tuple[Path, str, str]:
    """Build through official APIs and extract its pristine ``/testbed``.

    Cache-first: if the official source is already extracted on disk, return it
    without calling ``make_test_spec`` (which fetches from raw.githubusercontent.com).
    This avoids GitHub SSL errors when the image and testbed are already cached.
    """
    import docker as _docker

    # ── Cache-first path: avoid make_test_spec (GitHub fetch) if testbed exists ──
    # Scan _official_source for a cached testbed matching this instance_id.
    # The cache key is <instance_id>/<image_sha_prefix>/testbed.
    cache_base = prepared_root / "_official_source" / instance["instance_id"]
    if cache_base.exists():
        for img_dir in sorted(cache_base.iterdir(), reverse=True):
            testbed = img_dir / "testbed"
            if testbed.exists():
                # Reconstruct image_key from Docker by matching the sha prefix
                client = _docker.from_env()
                try:
                    candidates = client.images.list()
                    image_key = None
                    image_id = None
                    sha_prefix = img_dir.name
                    for img in candidates:
                        if img.id and sha_prefix in img.id:
                            for tag in img.tags:
                                if "sweb.eval" in tag and instance["instance_id"].lower().replace("__", "_") in tag.lower():
                                    image_key = tag.split(":")[0] if ":" in tag else tag
                                    image_id = img.id
                                    break
                            if image_key:
                                break
                    if image_key and image_id:
                        client.close()
                        return testbed, image_key, image_id
                finally:
                    client.close()

    # ── Slow path: build via official APIs ──
    client, spec = _docker_and_spec(instance, namespace)
    try:
        image = client.images.get(spec.instance_image_key)
    except Exception:
        image = None
    if image is not None:
        image_id = image.id
        repo = (
            prepared_root
            / "_official_source"
            / spec.instance_id
            / image_id.removeprefix("sha256:")[:16]
            / "testbed"
        )
        if repo.exists():
            client.close()
            return repo, spec.instance_image_key, image_id

    from swebench.harness.docker_build import build_container, build_env_images
    from swebench.harness.run_evaluation import close_logger, setup_logger

    if not spec.is_remote_image:
        _, failed = build_env_images(client, [spec], max_workers=1)
        if failed:
            client.close()
            raise RuntimeError(f"official SWE-bench environment build failed: {failed}")

    run_id = f"xenon-prepare-{uuid.uuid4().hex[:10]}"
    log_path = prepared_root / "runtime-logs" / spec.instance_id / "prepare.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    build_logger = setup_logger(spec.instance_id, log_path)
    container = None
    try:
        container = build_container(
            spec, client, run_id, build_logger,
            nocache=False, force_rebuild=False,
        )
        image_id = container.image.id
        image_key = spec.instance_image_key
        source = (
            prepared_root
            / "_official_source"
            / spec.instance_id
            / image_id.removeprefix("sha256:")[:16]
        )
        repo = source / "testbed"
        if not repo.exists():
            temporary = source.with_name(f"{source.name}.tmp-{uuid.uuid4().hex[:8]}")
            _extract_testbed(container, temporary)
            source.parent.mkdir(parents=True, exist_ok=True)
            temporary.rename(source)
        return repo, image_key, image_id
    finally:
        if container is not None:
            container.remove(force=True)
        close_logger(build_logger)
        client.close()


@dataclass(slots=True)
class OfficialTaskRuntime:
    host_worktree: Path
    container_name: str
    container_workdir: str
    image_key: str
    image_id: str
    _client: Any
    _container: Any

    @property
    def tool_runtime(self) -> ToolRuntime:
        return ToolRuntime(
            workspace_root=self.host_worktree,
            command_prefix=(
                "docker", "exec", "-w", self.container_workdir, self.container_name,
            ),
            backend_workdir=self.container_workdir,
            command_prelude=(
                "source /opt/miniconda3/etc/profile.d/conda.sh "
                "&& conda activate testbed"
            ),
        )

    def close(self) -> None:
        try:
            self._container.remove(force=True)
        finally:
            self._client.close()

    def __enter__(self) -> "OfficialTaskRuntime":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def create_official_runtime(
    instance: dict[str, Any],
    prepared_root: Path,
    engine_name: str,
    *,
    namespace: str | None = "swebench",
) -> OfficialTaskRuntime:
    """Create one isolated engine worktree backed by the official image env."""

    source, image_key, image_id = prepare_official_source(
        instance, prepared_root, namespace=namespace
    )
    worktree = prepared_root / instance["instance_id"] / engine_name
    if worktree.exists():
        shutil.rmtree(worktree)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, worktree, symlinks=True)

    # ── v0.8.3: 应用 test_patch（主流 agent 评测做法）──
    # SWE-bench 官方镜像的 /testbed 是原始仓库，FAIL_TO_PASS 测试的
    # 失败断言不在其中——agent 跑测试全部通过，验证循环的「测试失败」
    # 信号永不出现。OpenHands/SWE-agent 等主流框架在 agent 运行时都会
    # 应用 test_patch，让 FAIL_TO_PASS 测试真实失败：
    #   agent 修代码 → 跑测试失败 → 验证循环反馈失败详情 → 再修 → 通过
    # 应用后立即 commit，使最终 git diff 只反映 agent 的修改。
    test_patch = instance.get("test_patch") or ""
    if test_patch.strip():
        _apply_test_patch(worktree, test_patch, instance["instance_id"])

    client, spec = _docker_and_spec(instance, namespace)
    name = f"sweb.xenon.{spec.instance_id.lower()}.{engine_name}.{uuid.uuid4().hex[:8]}"
    container = client.containers.create(
        image=image_key,
        name=name,
        # Match the host worktree owner so git and files created through
        # docker exec remain usable on both sides of the bind mount.
        user=f"{os.getuid()}:{os.getgid()}",
        detach=True,
        command="tail -f /dev/null",
        platform=spec.platform,
        working_dir="/testbed",
        volumes={str(worktree): {"bind": "/testbed", "mode": "rw"}},
    )
    try:
        container.start()
        # The grader-only artifacts are injected by run_evaluation later.  If
        # an image ever begins shipping them, fail closed instead of leaking.
        probe = container.exec_run(
            ["/bin/bash", "-lc", "test ! -e /eval.sh && test ! -e /testbed/eval.sh"]
        )
        if probe.exit_code != 0:
            raise RuntimeError("grader artifacts are visible inside agent runtime")
        return OfficialTaskRuntime(
            host_worktree=worktree,
            container_name=name,
            container_workdir="/testbed",
            image_key=image_key,
            image_id=image_id,
            _client=client,
            _container=container,
        )
    except Exception:
        container.remove(force=True)
        client.close()
        raise
