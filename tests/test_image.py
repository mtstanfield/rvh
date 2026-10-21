"""scripts/image.py — the one worker image and its content-hash tag."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import image


@pytest.fixture
def context(tmp_path, monkeypatch):
    """A throwaway Dockerfile + copied files so tag tests don't depend on the real ones."""
    (tmp_path / "docker").mkdir()
    dockerfile = tmp_path / "docker" / "worker.Dockerfile"
    dockerfile.write_text("FROM scratch\n")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in image.IMAGE_FILES:
        (scripts / name).write_text(f"# {name}\n")
    monkeypatch.setattr(image, "DOCKERFILE", dockerfile)
    monkeypatch.setattr(image, "SCRIPT_DIR", scripts)
    return tmp_path


def test_tag_is_stable(context):
    assert image.worker_image_tag() == image.worker_image_tag()
    assert image.worker_image_tag().startswith("rvh-worker:")


def test_tag_changes_when_the_dockerfile_changes(context):
    before = image.worker_image_tag()
    (context / "docker" / "worker.Dockerfile").write_text("FROM scratch\nRUN true\n")
    assert image.worker_image_tag() != before


def test_tag_changes_when_a_copied_file_changes(context):
    before = image.worker_image_tag()
    (context / "scripts" / "llm_proxy.py").write_text("# changed\n")
    assert image.worker_image_tag() != before


def test_existing_image_is_not_rebuilt(context, monkeypatch):
    calls = []
    monkeypatch.setattr(image.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or MagicMock(returncode=0))
    tag = image.ensure_worker_image(log=lambda msg: None)
    assert calls == [["docker", "image", "inspect", tag]]


def test_missing_image_is_built(context, monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return MagicMock(returncode=1 if cmd[1] == "image" else 0, stderr="")

    monkeypatch.setattr(image.subprocess, "run", fake_run)
    tag = image.ensure_worker_image(log=lambda msg: None)
    assert calls[1][:2] == ["docker", "build"] and tag in calls[1]


def test_failed_build_raises(context, monkeypatch):
    monkeypatch.setattr(image.subprocess, "run", lambda cmd, **kw: MagicMock(returncode=1, stderr="boom"))
    with pytest.raises(image.ImageBuildError, match="boom"):
        image.ensure_worker_image(log=lambda msg: None)


def test_real_dockerfile_copies_exactly_the_hashed_files():
    """The COPY line and IMAGE_FILES must agree, or the tag misses a change."""
    copy = next(l for l in image.DOCKERFILE.read_text().splitlines() if l.startswith("COPY "))
    assert sorted(copy.split()[1:-1]) == sorted(image.IMAGE_FILES)
