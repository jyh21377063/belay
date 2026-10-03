"""eval.prepare：原生格式任务的镜像里没有 git 时，改用只多装了 git 的派生镜像（不调真的 docker）。"""
from __future__ import annotations

import subprocess

from eval import prepare as P

TOML = """schema_version = "1.1"

[agent]
timeout_sec = 10800.0

[environment]
allow_internet = false
docker_image = "zli12321/lhtb-spot-scheduler-traces:20260615"
cpus = 2
"""


class FakeDocker:
    def __init__(self, has_git: bool, pulled: bool = True, built: bool = False, user: str = ""):
        self.has_git, self.pulled, self.built, self.user = has_git, pulled, built, user
        self.calls: list[tuple] = []
        self.dockerfile = ""

    def __call__(self, *args, input=None, check=False):
        self.calls.append(args)
        rc, out = 0, ""
        if args[:2] == ("image", "inspect") and "-f" in args:
            out = self.user
        elif args[:2] == ("image", "inspect"):
            img = args[2]
            rc = 0 if (img.startswith(P.GIT_IMAGE_PREFIX) and self.built) or \
                (not img.startswith(P.GIT_IMAGE_PREFIX) and self.pulled) else 1
        elif args[0] == "pull":
            self.pulled = True
        elif args[0] == "run":
            rc = 0 if self.has_git else 1
        elif args[0] == "build":
            self.dockerfile, self.built = input, True
        return subprocess.CompletedProcess(args, rc, out, "")


def test_image_without_git_gets_a_derived_image(tmp_path, monkeypatch):
    toml = tmp_path / "task.toml"
    toml.write_text(TOML)
    fake = FakeDocker(has_git=False, pulled=False, user="app")
    monkeypatch.setattr(P, "_docker", fake)
    image = P.ensure_git_image(toml)
    assert image == "belay-local/zli12321_lhtb-spot-scheduler-traces:20260615-git"
    assert f'docker_image = "{image}"' in toml.read_text() and "cpus = 2" in toml.read_text()
    assert ("pull", "zli12321/lhtb-spot-scheduler-traces:20260615") in fake.calls
    assert fake.dockerfile.startswith("FROM zli12321/lhtb-spot-scheduler-traces:20260615\nUSER root\n")
    assert "apt-get install -y --no-install-recommends git" in fake.dockerfile
    assert fake.dockerfile.rstrip().endswith("USER app")           # 原来的用户保持不变
    assert "base: zli12321" in (tmp_path / "belay_git_image.txt").read_text()
    assert P.ensure_git_image(toml) is None                         # 再跑一次：已经是派生镜像


def test_image_with_git_is_left_alone(tmp_path, monkeypatch):
    toml = tmp_path / "task.toml"
    toml.write_text(TOML)
    fake = FakeDocker(has_git=True)
    monkeypatch.setattr(P, "_docker", fake)
    assert P.ensure_git_image(toml) is None and toml.read_text() == TOML
    assert not any(c[0] == "build" for c in fake.calls)


def test_an_existing_derived_image_is_reused(tmp_path, monkeypatch):
    toml = tmp_path / "task.toml"
    toml.write_text(TOML)
    fake = FakeDocker(has_git=False, built=True)
    monkeypatch.setattr(P, "_docker", fake)
    assert P.ensure_git_image(toml).endswith(":20260615-git")
    assert not any(c[0] == "build" for c in fake.calls)


def test_no_prebuilt_image_means_nothing_to_do(tmp_path, monkeypatch):
    toml = tmp_path / "task.toml"
    toml.write_text('[environment]\nallow_internet = false\n')
    monkeypatch.setattr(P, "_docker", FakeDocker(has_git=False))
    assert P.ensure_git_image(toml) is None


def test_git_image_names():
    assert P.git_image_name("ubuntu") == "belay-local/ubuntu:latest-git"
    assert P.git_image_name("localhost:5000/x/y") == "belay-local/localhost_5000_x_y:latest-git"
    assert P.git_image_name("A/B:1.2") == "belay-local/a_b:1.2-git"
