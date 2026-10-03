"""Publishing, against a local git repository standing in for GitHub."""

from __future__ import annotations

import json
import subprocess

import pytest

from conftest import EXAMPLES
from te_app_creator import publish, render
from te_app_creator.spec import load


class FakeGitHub:
    def __init__(self, exists: bool, released_tags=(), merges=((200, {"sha": "m1"}),)):
        self.exists = exists
        self.released_tags = set(released_tags)
        self.merges = list(merges)  # what each merge attempt gets back
        self.calls = []

    def repo(self, full_name):
        return {"default_branch": "main"} if self.exists else None

    def request(self, method, path, data=None):
        self.calls.append((method, path, data))
        if "/git/ref/tags/" in path:
            return (200, {}) if path.rsplit("/", 1)[1] in self.released_tags else (404, None)
        if path.endswith("/merge"):
            return self.merges.pop(0)
        return 200, {}

    def call(self, method, path, data=None, ok=(200, 201, 204)):
        self.calls.append((method, path, data))
        if method == "GET" and "/pulls" in path:
            return []
        if method == "POST" and path.endswith("/pulls"):
            return {"html_url": "https://github.com/example/pull/1", "number": 1}
        return {}


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def remote(tmp_path, monkeypatch):
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    monkeypatch.setattr(publish.Git, "remote", lambda self, full_name: str(bare))
    return bare


def minimal_spec(schema, defaults):
    return load([p for p in EXAMPLES if p.name == "minimal.yml"][0], schema, defaults)


def test_create_pushes_every_file_with_its_mode(schema, defaults, remote, tmp_path, monkeypatch):
    spec = minimal_spec(schema, defaults)
    fake = FakeGitHub(exists=False)
    monkeypatch.setattr(publish, "GitHub", lambda token: fake)
    result = publish.publish_interface(spec, "jupyter", defaults, "token", log=lambda m: None)
    assert result.action == "created"
    assert ("POST", f"/orgs/{defaults['github_org']}/repos") in [(m, p) for m, p, _ in fake.calls]
    # the release its own workflows now make, which the app creator follows
    assert result.tag == "v0.1.0"
    assert result.sha == git(remote, "rev-parse", "main").strip()

    listing = git(remote, "ls-tree", "-r", "main")
    assert "100755 blob" in [l for l in listing.splitlines() if l.endswith("template/script.sh.erb")][0]
    assert ".app-creator.json" in listing


def test_update_replaces_generated_files_and_keeps_others(schema, defaults, remote, tmp_path, monkeypatch):
    spec = minimal_spec(schema, defaults)
    # what the repository looks like before: an older render, one file that the
    # generator no longer makes, and one file a maintainer added by hand
    seed = tmp_path / "seed"
    seed.mkdir()
    files = render.render(spec, "jupyter", defaults)
    render.write(files, seed)
    (seed / "docker" / "obsolete.txt").write_text("generated once\n")
    (seed / "docker" / "extra-data.txt").write_text("added by hand\n")
    manifest = render.read_manifest(seed)
    manifest["files"].append("docker/obsolete.txt")
    (seed / render.MANIFEST).write_text(json.dumps(manifest))
    (seed / "README.md").write_text("an out of date README\n")
    git(seed, "init", "-q", "-b", "main")
    git(seed, "add", "-A")
    git(seed, "-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-q", "-m", "seed")
    git(seed, "push", "-q", str(remote), "main")

    fake = FakeGitHub(exists=True, released_tags={"v0.1.0"})
    monkeypatch.setattr(publish, "GitHub", lambda token: fake)
    result = publish.publish_interface(spec, "jupyter", defaults, "token", log=lambda m: None)
    assert result.action == "updated"

    branch = publish.UPDATE_BRANCH
    listing = git(remote, "ls-tree", "-r", "--name-only", branch).splitlines()
    assert "docker/extra-data.txt" in listing
    assert "docker/obsolete.txt" not in listing
    readme = git(remote, "show", f"{branch}:README.md")
    assert readme.startswith("# Minimal (JupyterLab)")

    pr = [d for m, p, d in fake.calls if m == "POST" and p.endswith("/pulls")][0]
    assert pr["head"] == branch and pr["base"] == "main"
    # v0.1.0 is already released, so the pull request says merging will not release it
    assert "already released" in pr["body"]


def seed_older_render(spec, defaults, remote, tmp_path):
    """The repository as an older version of the generator left it."""
    seed = tmp_path / "seed"
    render.write(render.render(spec, "jupyter", defaults), seed)
    (seed / "README.md").write_text("an out of date README\n")
    git(seed, "init", "-q", "-b", "main")
    git(seed, "add", "-A")
    git(seed, "-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-q", "-m", "seed")
    git(seed, "push", "-q", str(remote), "main")


def test_an_approved_update_is_merged_and_released(schema, defaults, remote, tmp_path, monkeypatch):
    spec = minimal_spec(schema, defaults)
    seed_older_render(spec, defaults, remote, tmp_path)
    fake = FakeGitHub(exists=True)
    monkeypatch.setattr(publish, "GitHub", lambda token: fake)
    result = publish.publish_interface(spec, "jupyter", defaults, "token", merge=True, log=lambda m: None)

    assert (result.action, result.tag, result.sha) == ("merged", "v0.1.0", "m1")
    full = f"{defaults['github_org']}/training-environment-jupyter-minimal-app"
    merge = [d for m, p, d in fake.calls if p == f"/repos/{full}/pulls/1/merge"][0]
    assert merge["merge_method"] == "squash"
    assert ("DELETE", f"/repos/{full}/git/refs/heads/{publish.UPDATE_BRANCH}") in [(m, p) for m, p, _ in fake.calls]
    body = [d for m, p, d in fake.calls if m == "POST" and p.endswith("/pulls")][0]["body"]
    assert "merges this pull request itself" in body


def test_a_merged_update_of_a_released_version_releases_nothing(schema, defaults, remote, tmp_path, monkeypatch):
    spec = minimal_spec(schema, defaults)
    seed_older_render(spec, defaults, remote, tmp_path)
    monkeypatch.setattr(publish, "GitHub", lambda token: FakeGitHub(exists=True, released_tags={"v0.1.0"}))
    result = publish.publish_interface(spec, "jupyter", defaults, "token", merge=True, log=lambda m: None)
    assert (result.action, result.tag) == ("merged", "")
    assert "already released" in result.detail


def test_merging_waits_while_github_works_out_whether_it_can():
    fake = FakeGitHub(exists=True, merges=[(405, {"message": "not mergeable yet"}), (409, {}), (200, {"sha": "m2"})])
    waits = []
    assert publish._merge(fake, "org/app", 3, "Update", log=lambda m: None, sleep=waits.append) == "m2"
    assert waits == [2, 4]

    stuck = FakeGitHub(exists=True, merges=[(422, {"message": "merge conflict"})])
    with pytest.raises(publish.PublishError, match="merge conflict"):
        publish._merge(stuck, "org/app", 3, "Update", log=lambda m: None, sleep=waits.append)


def test_update_with_nothing_to_change(schema, defaults, remote, tmp_path, monkeypatch):
    spec = minimal_spec(schema, defaults)
    seed = tmp_path / "seed"
    render.write(render.render(spec, "jupyter", defaults), seed)
    git(seed, "init", "-q", "-b", "main")
    git(seed, "add", "-A")
    git(seed, "-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-q", "-m", "seed")
    git(seed, "push", "-q", str(remote), "main")

    monkeypatch.setattr(publish, "GitHub", lambda token: FakeGitHub(exists=True))
    result = publish.publish_interface(spec, "jupyter", defaults, "token", log=lambda m: None)
    assert result.action == "unchanged"


def test_a_repository_the_app_creator_did_not_make_is_left_alone(schema, defaults, remote, tmp_path, monkeypatch):
    spec = minimal_spec(schema, defaults)
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "README.md").write_text("someone else's app\n")
    git(seed, "init", "-q", "-b", "main")
    git(seed, "add", "-A")
    git(seed, "-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-q", "-m", "seed")
    git(seed, "push", "-q", str(remote), "main")

    monkeypatch.setattr(publish, "GitHub", lambda token: FakeGitHub(exists=True))
    with pytest.raises(publish.PublishError, match="not made by the app creator"):
        publish.publish_interface(spec, "jupyter", defaults, "token", log=lambda m: None)


def test_dry_run_changes_nothing(schema, defaults, remote, monkeypatch):
    spec = minimal_spec(schema, defaults)
    fake = FakeGitHub(exists=False)
    monkeypatch.setattr(publish, "GitHub", lambda token: fake)
    result = publish.publish_interface(spec, "jupyter", defaults, "token", dry_run=True)
    assert result.action == "would-create"
    assert not [c for c in fake.calls if c[0] != "GET"]
