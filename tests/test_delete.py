"""Deleting an app: only what the app creator made for it, and its spec."""

from __future__ import annotations

import base64
import json

from te_app_creator import cli, delete, publish

ORG = "reannz-training-environment"
CREATOR = f"{ORG}/training-environment-app-creator"


def manifest(app, generator="training-environment-app-creator"):
    text = json.dumps({"generator": generator, "app": app, "interface": "jupyter"})
    return {"content": base64.b64encode(text.encode()).decode()}


class FakeGitHub(publish.GitHub):
    """Repositories, images and specs, in memory."""

    def __init__(self, repos=None, packages=(), specs=(), sees_images=True):
        super().__init__("token")
        self.repos = dict(repos or {})  # name -> manifest payload, or None for a repository without one
        self.packages = set(packages)
        self.specs = set(specs)
        # a GitHub App's token is refused by GitHub's API for images
        self.sees_images = sees_images
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
        if path == f"/repos/{ORG}/{name}":
            if name not in self.repos:
                return 404, None
            if method == "GET":
                return 200, {"default_branch": "main"}
            if method == "DELETE":
                del self.repos[name]
                return 204, None
        if path.startswith(f"/repos/{ORG}/{name}/contents/.app-creator.json"):
            found = self.repos.get(name)
            return (200, found) if found else (404, None)
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


def run(gh, name="test2", dry_run=False, packages=None, defaults=None):
    return delete.delete_app(name, defaults, CREATOR, gh, packages=packages, dry_run=dry_run, log=lambda m: None)


def repo(interface, app="test2"):
    return f"training-environment-{interface}-{app}-app"


def test_an_app_is_deleted_with_its_images_and_spec(defaults):
    gh = FakeGitHub(
        repos={repo("jupyter"): manifest("test2"), repo("rstudio"): manifest("test2")},
        packages={repo("jupyter"), repo("rstudio")},
        specs={"apps/test2.yml"},
    )
    removals = run(gh, defaults=defaults)
    assert not gh.repos and not gh.packages and not gh.specs
    assert [(r.what, r.result) for r in removals] == [
        ("repository", "deleted"),
        ("image", "deleted"),
        ("repository", "deleted"),
        ("image", "deleted"),
        ("image", "not found"),  # there is no VS Code repository, nor image
        ("spec", "deleted"),
    ]
    text = delete.markdown("test2", removals, dry_run=False)
    assert text.startswith("## Deleted the test2 app")
    assert "| Spec | `apps/test2.yml` | :wastebasket: deleted |" in text
    assert "restore them" in text


def test_a_dry_run_deletes_nothing(defaults):
    gh = FakeGitHub(repos={repo("jupyter"): manifest("test2")}, packages={repo("jupyter")}, specs={"apps/test2.yml"})
    removals = run(gh, dry_run=True, defaults=defaults)
    assert not [c for c in gh.calls if c[0] == "DELETE"]
    assert [(r.what, r.result) for r in removals][:2] == [("repository", "would delete"), ("image", "would delete")]
    assert removals[-1].result == "would delete"
    assert delete.markdown("test2", removals, dry_run=True).startswith("## Dry run: what deleting the test2 app would do")


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
    assert len(gh.repos) == 3 and len(gh.packages) == 3
    assert [r.result for r in removals if r.what == "repository"] == ["kept"] * 3
    assert not [r for r in removals if r.what == "image"]
    assert removals[-1] == delete.Removal("spec", "apps/test2.yml", "not found")


def test_images_the_app_may_not_delete_are_left_to_delete_by_hand(defaults):
    gh = FakeGitHub(repos={repo("jupyter"): manifest("test2")}, packages={repo("jupyter")}, specs={"apps/test2.yml"}, sees_images=False)
    removals = run(gh, defaults=defaults)
    # the other interfaces had no repository, and the App cannot see whether
    # they left images, so they are not listed
    assert [(r.what, r.result) for r in removals] == [("repository", "deleted"), ("image", "by hand"), ("spec", "deleted")]
    assert f"https://github.com/orgs/{ORG}/packages/container/{repo('jupyter')}/settings" in removals[1].detail
    text = delete.markdown("test2", removals, dry_run=False)
    assert text.startswith("## Deleted the test2 app, apart from its images")
    assert "| Image | `ghcr.io/reannz-training-environment/training-environment-jupyter-test2-app` | :point_right: delete it by hand: from [its settings]" in text
    assert "APP_CREATOR_PACKAGES_TOKEN" in text

    # with a token that may, they are deleted too
    packages = FakeGitHub(packages={repo("jupyter"), repo("rstudio")})
    removals = run(FakeGitHub(repos={repo("jupyter"): manifest("test2")}), packages=packages, defaults=defaults)
    assert not packages.packages
    assert [r.result for r in removals if r.what == "image"] == ["deleted", "deleted", "not found"]


def test_leaving_images_to_delete_by_hand_is_not_a_failure(monkeypatch, defaults):
    gh = FakeGitHub(repos={repo("jupyter"): manifest("test2")}, packages={repo("jupyter")}, sees_images=False)
    monkeypatch.setattr(cli.publisher, "GitHub", lambda token: gh)
    monkeypatch.setenv("APP_CREATOR_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", CREATOR)
    monkeypatch.delenv("PACKAGES_TOKEN", raising=False)
    assert cli.main(["delete", "--app", "test2", "--confirm", "test2"]) == 0


def test_the_name_must_be_typed_twice(monkeypatch, capsys):
    monkeypatch.setenv("APP_CREATOR_TOKEN", "token")
    assert cli.main(["delete", "--app", "test2", "--confirm", "test3"]) == 1
    assert "do not match" in capsys.readouterr().err
    assert cli.main(["delete", "--app", "../evil", "--confirm", "../evil"]) == 1
    assert "not an app name" in capsys.readouterr().err


def test_the_command_deletes_and_reports(monkeypatch, tmp_path, defaults):
    gh = FakeGitHub(repos={repo("jupyter"): manifest("test2")}, packages={repo("jupyter")}, specs={"apps/test2.yml"})
    monkeypatch.setattr(cli.publisher, "GitHub", lambda token: gh)
    monkeypatch.setenv("APP_CREATOR_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", CREATOR)
    monkeypatch.delenv("PACKAGES_TOKEN", raising=False)
    report = tmp_path / "report.md"
    assert cli.main(["delete", "--app", "test2", "--confirm", "test2", "--markdown", str(report)]) == 0
    assert report.read_text().startswith("## Deleted the test2 app")
    assert not gh.repos and not gh.specs
