"""Path containment in the runner: file ops can only touch paths inside
$HOME (or CT_RUNNER_FS_ROOTS). Defence-in-depth on top of HMAC + Tailscale
so a compromised bridge / leaked HMAC secret can't read /etc/passwd or
write to /tmp."""

from __future__ import annotations

import pathlib

import pytest

from ct.runner.server import _contain, _resolve_fs_roots


def test_paths_inside_home_pass(tmp_path: pathlib.Path) -> None:
    home = pathlib.Path.home().resolve()
    # Use a subpath of $HOME so the resolve check actually has something
    # legitimate to match against.
    assert _contain(home).is_absolute()
    # A file path inside home — doesn't have to exist (resolve(strict=False)).
    inside = home / "code-in-tele-test-fixture-does-not-exist.txt"
    assert _contain(inside) == inside.resolve()


def test_etc_passwd_is_rejected() -> None:
    with pytest.raises(ValueError, match="outside allowed roots"):
        _contain(pathlib.Path("/etc/passwd"))


def test_traversal_via_dotdot_is_rejected() -> None:
    home = pathlib.Path.home()
    # ../../../etc/passwd from inside ~ should resolve to /etc/passwd → reject.
    sneaky = home / "subdir" / ".." / ".." / ".." / "etc" / "passwd"
    with pytest.raises(ValueError):
        _contain(sneaky)


def test_tmp_is_rejected_by_default() -> None:
    with pytest.raises(ValueError):
        _contain(pathlib.Path("/tmp/foo"))


def test_env_override_extends_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate: CT_RUNNER_FS_ROOTS=/tmp:/Users/ali — should now allow /tmp.
    monkeypatch.setenv("CT_RUNNER_FS_ROOTS", f"/tmp:{pathlib.Path.home()}")
    roots = _resolve_fs_roots()
    assert pathlib.Path("/tmp").resolve() in roots
    assert pathlib.Path.home().resolve() in roots
