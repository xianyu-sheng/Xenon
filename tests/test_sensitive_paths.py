"""#449: one answer to "is this path sensitive?" for every consumer."""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from xenon.utils.sensitive import (
    SYSTEM_PATHS,
    SYSTEM_WRITE_BLACKLIST,
    classify_path,
    matches_credential_name,
)

POSIX_WS = "/home/u/proj"
WIN_WS = r"D:\Xenon"

#: The write blacklist as it stood before #449.  It may only grow.
_WRITE_BLACKLIST_BEFORE_449 = (
    "c:\\windows",
    "c:\\program files",
    "c:\\programdata",
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/boot",
    "/dev",
    "/proc",
    "/sys",
    "/var/log",
    "/root/.ssh",
    "/root/.gnupg",
)


@pytest.mark.parametrize(
    "path",
    [
        "/home/u/proj/.env",
        "/home/u/proj/.env.local",
        r"D:\Xenon\.ENV",
        "/home/u/.ssh/id_rsa",
        "/home/u/.ssh/id_ed25519.pub",
        r"C:\Users\x\.ssh\ID_RSA",
        "/home/u/proj/certs/server.pem",
        r"D:\Xenon\certs\Server.PEM",
        "/home/u/proj/tls.key",
        "/home/u/proj/credentials.json",
        "/home/u/proj/secrets.yaml",
        r"C:\Users\x\.aws\credentials",
        "~/.aws/config",
        "/home/u/.aws/sso/cache/token.json",
        "/home/u/.kube/config",
        "/home/u/.git-credentials",
        "/home/u/proj/.netrc",
    ],
)
def test_credential(path: str) -> None:
    assert classify_path(path, workspace=POSIX_WS) == "credential"
    assert classify_path(path, workspace=WIN_WS) == "credential"
    assert classify_path(path) == "credential"


@pytest.mark.parametrize(
    "path",
    [
        r"C:\Windows",
        r"C:\Windows\System32\drivers\etc\hosts",
        "c:/windows/system32/cmd.exe",
        r"C:\WINDOWS\win.ini",
        r"C:\Program Files\App\app.exe",
        r"C:\ProgramData\x.ini",
        "/etc/passwd",
        "/etc",
        "/usr/lib/python3/os.py",
        "/usr/local/bin/tool",
        "/bin",
        "/bin/sh",
        "/sbin/init",
        "/boot/vmlinuz",
        "/dev/sda",
        "/proc/self/environ",
        "/sys/kernel",
        "/root/notes.txt",
        "/var/log/syslog",
        "/ETC/Passwd",
    ],
)
def test_system(path: str) -> None:
    assert classify_path(path, workspace=POSIX_WS) == "system"
    assert classify_path(path) == "system"


@pytest.mark.parametrize(
    ("path", "workspace"),
    [
        ("/home/u/other/a.txt", POSIX_WS),
        ("/home/u/proj-2/a.txt", POSIX_WS),
        ("/home/u/proj/../other/a.txt", POSIX_WS),
        ("../sibling/a.txt", POSIX_WS),
        ("~/notes.txt", POSIX_WS),
        (r"D:\elsewhere\a.txt", WIN_WS),
        (r"E:\Xenon\a.txt", WIN_WS),
        ("/tmp/scratch.txt", WIN_WS),
    ],
)
def test_outside_workspace(path: str, workspace: str) -> None:
    assert classify_path(path, workspace=workspace) == "outside"


@pytest.mark.parametrize(
    ("path", "workspace"),
    [
        ("/home/u/proj/src/main.py", POSIX_WS),
        ("/home/u/proj", POSIX_WS),
        ("/home/u/proj/", POSIX_WS),
        ("src/main.py", POSIX_WS),
        ("./README.md", POSIX_WS),
        # Directories that merely share a system name are not system paths.
        ("/home/u/proj/bin/run.sh", POSIX_WS),
        ("/home/u/proj/usr/share/x", POSIX_WS),
        ("/home/u/proj/etc/app.conf", POSIX_WS),
        ("/home/u/proj/dev/notes.md", POSIX_WS),
        (r"D:\Xenon\xenon\runtime.py", WIN_WS),
        (r"d:\xenon\XENON\Runtime.py", WIN_WS),
        ("D:/Xenon/docs/GUIDE.md", WIN_WS),
        # Names that only look similar are not credentials.
        ("/home/u/proj/environment.py", POSIX_WS),
        ("/home/u/proj/keyboard.py", POSIX_WS),
        ("/home/u/proj/docs/env.md", POSIX_WS),
    ],
)
def test_normal(path: str, workspace: str) -> None:
    assert classify_path(path, workspace=workspace) == ""


@pytest.mark.parametrize(
    "path", ["/binary/x", "/usrlocal/x", "/etcetera", "c:/windowsapps/x"]
)
def test_system_matches_whole_components(path: str) -> None:
    assert classify_path(path) == ""


def test_without_workspace_nothing_is_outside() -> None:
    assert classify_path("/home/u/other/a.txt") == ""
    assert classify_path(r"D:\elsewhere\a.txt") == ""


@pytest.mark.parametrize("path", ["", "   ", ".", None])
def test_empty_is_normal(path) -> None:
    assert classify_path(path, workspace=POSIX_WS) == ""


def test_path_objects_are_accepted() -> None:
    assert classify_path(PurePosixPath("/etc/hosts")) == "system"
    assert (
        classify_path(PureWindowsPath(r"C:\Users\x\.env"), workspace=Path("/x"))
        == "credential"
    )
    assert classify_path(PurePosixPath("/w/a.py"), workspace=PurePosixPath("/w")) == ""


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (".env", True),
        (".env.production", True),
        ("id_rsa", True),
        ("ID_ECDSA", True),
        ("cert.pem", True),
        ("store.p12", True),
        ("credentials", True),
        ("credentials.json", True),
        ("secret.txt", True),
        (".git-credentials", True),
        ("main.py", False),
        ("environment.yml", False),
        ("id_rsa_helper.py", False),
        ("pem.txt", False),
        ("", False),
    ],
)
def test_matches_credential_name(name: str, expected: bool) -> None:
    assert matches_credential_name(name) is expected


def test_write_blacklist_only_grows() -> None:
    """The write blacklist is a security boundary: #449 must not shrink it."""

    from xenon.nodes import tool_node

    assert set(_WRITE_BLACKLIST_BEFORE_449) <= set(SYSTEM_WRITE_BLACKLIST)
    assert tool_node._SENSITIVE_PATHS == list(SYSTEM_WRITE_BLACKLIST)
    # Everything refused for writes is worth attention when read.
    assert set(SYSTEM_WRITE_BLACKLIST) <= set(SYSTEM_PATHS)
    for entry in SYSTEM_WRITE_BLACKLIST:
        assert classify_path(entry) in {"system", "credential"}, entry
    assert classify_path("/root/.ssh/authorized_keys") == "credential"


def test_dashboard_uses_the_same_classifier() -> None:
    """The attention projection must not keep a private copy of the lists."""

    from xenon.frontend import queries

    assert queries._SENSITIVE_SYSTEM_TOKENS is SYSTEM_PATHS
    for path, workspace in [
        ("/etc/passwd", WIN_WS),
        (r"D:\Xenon\.env", WIN_WS),
        (r"D:\elsewhere\a.txt", WIN_WS),
        ("/home/u/proj/bin/run.sh", POSIX_WS),
        ("src/a.py", WIN_WS),
    ]:
        assert queries._is_sensitive_path(path, workspace) == classify_path(
            path, workspace=workspace
        )
