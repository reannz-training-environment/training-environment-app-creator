"""Deleting an app: only what the app creator made for it, its images, and its spec."""

from __future__ import annotations

import base64
import itertools
import json
import re

import pytest

from te_app_creator import cli, delete, publish

ORG = "reannz-training-environment"
CREATOR = f"{ORG}/training-environment-app-creator"


def manifest(app, generator="training-environment-app-creator"):
    text = json.dumps({"generator": generator, "app": app, "interface": "jupyter"})
    return {"content": base64.b64encode(text.encode()).decode()}


class FakeGitHub(publish.GitHub):
    """Repositories, images, specs and each repository's workflow runs, in memory.

    The workflow the app creator adds to a repository runs when the
    delete-image event arrives, and deletes the repository's image, if it
    has one, the way the real one does. `workflow` is "works", "fails" or
    "never starts"; `drop` is how many events are lost while GitHub is still
    noticing the new workflow."""

    def __init__(self, repos=None, packages=(), specs=(), sees_images=True, workflow="works", drop=0):
        super().__init__("token")
        self.repos = dict(repos or {})  # name -> manifest payload, or None for a repository without one
        self.packages = set(packages)
        self.specs = set(specs)
        self.sees_images = sees_images  # a GitHub App's token is refused by the API for images
        self.workflow = workflow
        self.drop = drop
        self.added = {}  # repository -> commit message of the added workflow
        self.runs = {}  # repository -> its delete-image runs
        self.ids = itertools.count(1000)
        self.calls = []

    def request(self, method, path, data=None):
        self.calls.append((method, path, data))
        name = path.split("/")[3] if path.startswith(f"/repos/{ORG}/") else ""
        if path == f"/repos/{CREATOR}" and method == "GET":
            return 200, {"default_branch": "main"}
        if path.startswith(f"/repos/{CREATOR}/contents/apps/"):
            spec = path.split("/contents/")[1].split("?")[0]
            if method == "GET":
                return (200, {"sha": "blob1"}) if spec in self.specs else (404, None)
            if method == "DELETE":
                assert data["sha"] == "blob1" and data["branch"] == "main"
                self.specs.discard(spec)
                return 200, {}
        if name and name not in self.repos:
            return 404, None
        if path == f"/repos/{ORG}/{name}":
            if method == "GET":
                return 200, {"default_branch": "main"}
            if method == "DELETE":
                del self.repos[name]
                return 204, None
        if path.startswith(f"/repos/{ORG}/{name}/contents/.app-creator.json"):
            found = self.repos.get(name)
            return (200, found) if found else (404, None)
        if path.startswith(f"/repos/{ORG}/{name}/contents/{delete.IMAGE_WORKFLOW}"):
            if method == "GET":
                return (200, {"sha": "wf1"}) if name in self.added else (404, None)
            assert method == "PUT" and data["branch"] == "main"
            assert base64.b64decode(data["content"]).decode() == delete.IMAGE_WORKFLOW_TEXT
            self.added[name] = data["message"]
            return 201, {}
        if path == f"/repos/{ORG}/{name}/dispatches":
            assert data == {"event_type": "delete-image"}
            if self.drop or self.workflow == "never starts" or name not in self.added:
                self.drop = max(0, self.drop - 1)
                return 204, None
            # the workflow runs: it deletes the image if there is one
            package = name.lower()
            existed = package in self.packages
            if self.workflow == "works":
                self.packages.discard(package)
            run = {
                "id": next(self.ids),
                "path": delete.IMAGE_WORKFLOW,
                "status": "queued",
                "conclusion": None,
                "html_url": f"https://github.com/{ORG}/{name}/actions/runs/1",
                "result": "failure" if self.workflow == "fails" else "success",
                "step": "success" if existed else "skipped",
            }
            self.runs.setdefault(name, []).insert(0, run)
            return 204, None
        if path.startswith(f"/repos/{ORG}/{name}/actions/runs?event=repository_dispatch"):
            return 200, {"workflow_runs": list(self.runs.get(name, []))}
        if r := re.fullmatch(rf"/repos/{ORG}/{name}/actions/runs/(\d+)(/jobs)?", path):
            run = next(run for run in self.runs[name] if run["id"] == int(r.group(1)))
            if r.group(2):
                steps = [{"name": "Find the image", "conclusion": "success"}, {"name": "Delete the image", "conclusion": run["step"]}]
                return 200, {"jobs": [{"steps": steps}]}
            run.update(status="completed", conclusion=run["result"])
            return 200, dict(run)
        if path.startswith(f"/orgs/{ORG}/packages/container/"):
            package = path.rsplit("/", 1)[1]
            if not self.sees_images:
                return 403, {"message": "Resource not accessible by integration"}
            if package not in self.packages:
                return 404, {"message": "Package not found."}
            if method == "DELETE":
                self.packages.discard(package)
                return 204, None
            return 200, {}
        raise AssertionError(f"unexpected call {method} {path}")


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def run(gh, name="test2", dry_run=False, packages=None, defaults=None):
    clock = Clock()
    return delete.delete_app(
        name, defaults, CREATOR, gh, packages=packages, runs=gh,
        dry_run=dry_run, log=lambda m: None, clock=clock, sleep=clock.sleep,
    )


def repo(interface, app="test2"):
    return f"training-environment-{interface}-{app}-app"


def image(interface, app="test2"):
    return f"ghcr.io/{ORG}/{repo(interface, app)}"


def test_each_repository_deletes_its_own_image_before_it_is_deleted(defaults):
    gh = FakeGitHub(
        repos={repo("jupyter"): manifest("test2"), repo("rstudio"): manifest("test2")},
        packages={repo("jupyter"), repo("rstudio")},
        specs={"apps/test2.yml"},
        sees_images=False,  # the App: refused by the API for images
    )
    removals = run(gh, defaults=defaults)
    assert not gh.repos and not gh.packages and not gh.specs
    assert [(r.what, r.result) for r in removals] == [
        ("repository", "deleted"),
        ("image", "deleted"),
        ("repository", "deleted"),
        ("image", "deleted"),
        ("spec", "deleted"),
    ]
    # the App never asks the API for images, and adding the workflow does not
    # start the repository's image build
    assert not [c for c in gh.calls if "/packages/" in c[1]]
    assert all("[skip ci]" in message for message in gh.added.values())
    # each image goes before its repository, whose workflows delete it
    calls = [(m, p) for m, p, _ in gh.calls]
    for name in (repo("jupyter"), repo("rstudio")):
        assert calls.index(("POST", f"/repos/{ORG}/{name}/dispatches")) < calls.index(("DELETE", f"/repos/{ORG}/{name}"))

    text = delete.markdown("test2", removals, dry_run=False)
    assert text.startswith("## Deleted the test2 app\n")
    assert f"| Image | `{image('jupyter')}` | :wastebasket: deleted |" in text
    assert "| Spec | `apps/test2.yml` | :wastebasket: deleted |" in text


def test_a_repository_without_an_image_says_so(defaults):
    gh = FakeGitHub(repos={repo("jupyter"): manifest("test2")}, packages=(), sees_images=False)
    removals = run(gh, defaults=defaults)
    assert [(r.what, r.result) for r in removals][:2] == [("repository", "deleted"), ("image", "not found")]


def test_an_event_lost_while_github_notices_the_new_workflow_is_sent_again(defaults):
    gh = FakeGitHub(repos={repo("jupyter"): manifest("test2")}, packages={repo("jupyter")}, sees_images=False, drop=2)
    removals = run(gh, defaults=defaults)
    assert [(r.what, r.result) for r in removals][:2] == [("repository", "deleted"), ("image", "deleted")]
    assert len([c for c in gh.calls if c[0] == "POST" and c[1].endswith("/dispatches")]) == 3


@pytest.mark.parametrize("workflow, why", [("fails", "could not delete it"), ("never starts", "did not start")])
def test_an_image_its_repository_cannot_delete_is_left_to_delete_by_hand(defaults, workflow, why):
    gh = FakeGitHub(repos={repo("jupyter"): manifest("test2")}, packages={repo("jupyter")}, sees_images=False, workflow=workflow)
    removals = run(gh, defaults=defaults)
    assert [(r.what, r.result) for r in removals][:2] == [("repository", "deleted"), ("image", "by hand")]
    assert why in removals[1].detail
    assert f"https://github.com/orgs/{ORG}/packages/container/{repo('jupyter')}/settings" in removals[1].detail
    assert delete.markdown("test2", removals, dry_run=False).startswith("## Deleted the test2 app, apart from some images")


def test_a_dry_run_changes_nothing(defaults):
    gh = FakeGitHub(repos={repo("jupyter"): manifest("test2")}, packages={repo("jupyter")}, specs={"apps/test2.yml"}, sees_images=False)
    removals = run(gh, dry_run=True, defaults=defaults)
    assert not [c for c in gh.calls if c[0] in ("PUT", "POST", "DELETE")]
    assert [(r.what, r.result) for r in removals] == [("repository", "would delete"), ("image", "would delete"), ("spec", "would delete")]
    assert removals[1].detail == "by its repository's own workflow"
    assert delete.markdown("test2", removals, dry_run=True).startswith("## Dry run: what deleting the test2 app would do")


def test_with_a_token_for_images_it_deletes_them_itself(defaults):
    gh = FakeGitHub(repos={repo("jupyter"): manifest("test2")}, sees_images=False)
    # a personal access token (classic), which also finds the image of a
    # repository already deleted
    packages = FakeGitHub(packages={repo("jupyter"), repo("rstudio")})
    removals = run(gh, packages=packages, defaults=defaults)
    assert not packages.packages and not gh.added
    assert [(r.what, r.result) for r in removals] == [
        ("repository", "deleted"),
        ("image", "deleted"),
        ("image", "deleted"),  # RStudio: its repository was already gone
        ("image", "not found"),  # VS Code: never made
        ("spec", "not found"),
    ]


def test_repositories_the_app_creator_did_not_make_are_left_alone(defaults):
    gh = FakeGitHub(
        repos={
            repo("jupyter"): None,  # made by hand
            repo("rstudio"): manifest("other-app"),  # somehow made for another app
            repo("codeserver"): manifest("test2", generator="something-else"),
        },
        packages={repo("jupyter"), repo("rstudio"), repo("codeserver")},
    )
    removals = run(gh, defaults=defaults)
    assert len(gh.repos) == 3 and len(gh.packages) == 3 and not gh.added
    assert [r.result for r in removals if r.what == "repository"] == ["kept"] * 3
    assert not [r for r in removals if r.what == "image"]
    assert removals[-1] == delete.Removal("spec", "apps/test2.yml", "not found")


def test_the_name_must_be_typed_twice(monkeypatch, capsys):
    monkeypatch.setenv("APP_CREATOR_TOKEN", "token")
    assert cli.main(["delete", "--app", "test2", "--confirm", "test3"]) == 1
    assert "do not match" in capsys.readouterr().err
    assert cli.main(["delete", "--app", "../evil", "--confirm", "../evil"]) == 1
    assert "not an app name" in capsys.readouterr().err


def test_the_command_deletes_and_reports(monkeypatch, tmp_path):
    gh = FakeGitHub(repos={repo("jupyter"): manifest("test2")}, packages={repo("jupyter")}, specs={"apps/test2.yml"}, sees_images=False)
    tokens = []
    monkeypatch.setattr(cli.publisher, "GitHub", lambda token: tokens.append(token) or gh)
    clock = Clock()
    monkeypatch.setattr(delete.time, "monotonic", clock)
    monkeypatch.setattr(delete.time, "sleep", clock.sleep)
    monkeypatch.setenv("APP_CREATOR_TOKEN", "app-token")
    monkeypatch.setenv("RUNS_TOKEN", "workflow-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", CREATOR)
    monkeypatch.delenv("PACKAGES_TOKEN", raising=False)
    report = tmp_path / "report.md"
    assert cli.main(["delete", "--app", "test2", "--confirm", "test2", "--markdown", str(report)]) == 0
    assert report.read_text().startswith("## Deleted the test2 app\n")
    assert not gh.repos and not gh.specs and not gh.packages
    assert tokens == ["app-token", "workflow-token"]
