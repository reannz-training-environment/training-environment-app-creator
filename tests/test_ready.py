"""Following released images until they are built, and checking they can be pulled."""

from __future__ import annotations

import io
import json
import urllib.error

from te_app_creator import ready


def run(path, branch, status="completed", conclusion="success", sha="s1", url="https://run"):
    return {"path": f".github/workflows/{path}", "head_branch": branch, "head_sha": sha, "status": status, "conclusion": conclusion, "html_url": url}


class Runs:
    """Each repository's workflow runs, as a script of what each poll sees."""

    def __init__(self, script):
        self.script = {repo: list(steps) for repo, steps in script.items()}

    def request(self, method, path, data=None):
        repo = path.split("/repos/")[1].split("/actions/")[0]
        steps = self.script[repo]
        runs = steps.pop(0) if len(steps) > 1 else steps[0]
        return 200, {"workflow_runs": runs}


def item(repo="org/training-environment-jupyter-x-app", interface="jupyter"):
    return {"repo": repo, "interface": interface, "tag": "v0.1.0", "sha": "s1", "image": f"ghcr.io/{repo}", "state": "waiting"}


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_a_release_is_followed_until_its_image_is_built(monkeypatch):
    monkeypatch.setattr(ready, "pullable", lambda image, tag: image.endswith("jupyter-x-app"))
    jupyter, rstudio = item(), item("org/training-environment-rstudio-x-app", "rstudio")
    gh = Runs(
        {
            jupyter["repo"]: [
                [run("release.yml", "main", status="in_progress")],
                [run("release.yml", "main"), run("build_container.yml", "v0.1.0", status="in_progress")],
                [run("build_container.yml", "v0.1.0", url="https://run/j")],
            ],
            rstudio["repo"]: [[run("build_container.yml", "v0.1.0", conclusion="failure", url="https://run/r")]],
        }
    )
    clock = Clock()
    items = ready.wait(gh, [jupyter, rstudio], timeout=3600, interval=30, log=lambda m: None, clock=clock, sleep=clock.sleep)
    assert [(i["state"], i.get("public")) for i in items] == [("built", True), ("failed", None)]
    assert jupyter["run_url"] == "https://run/j"
    assert clock.now == 60


def test_failures_before_the_build_are_caught():
    failed, silent, slow = item("org/a"), item("org/b"), item("org/c")
    gh = Runs(
        {
            "org/a": [[run("release.yml", "main", conclusion="failure")]],
            # tagged, but the build it should have started never appears
            "org/b": [[run("release.yml", "main")]],
            "org/c": [[run("build_container.yml", "v0.1.0", status="in_progress")]],
            # the main branch's own build is not this release's
        }
    )
    clock = Clock()
    ready.wait(gh, [failed, silent, slow], timeout=600, interval=60, log=lambda m: None, clock=clock, sleep=clock.sleep)
    assert [failed["state"], silent["state"], slow["state"]] == ["release failed", "not started", "timed out"]


def test_runs_for_other_commits_and_branches_do_not_count():
    it = item()
    gh = Runs({it["repo"]: [[run("build_container.yml", "main"), run("release.yml", "main", sha="older", conclusion="failure")]]})
    ready.poll(gh, it, now=0)
    assert it["state"] == "waiting"


class Response(io.BytesIO):
    def __init__(self, body=b"", status=200):
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_an_image_is_public_when_it_can_be_pulled_without_credentials():
    seen = []

    def public(req, timeout):
        seen.append(req if isinstance(req, str) else (req.get_method(), req.full_url, req.headers["Authorization"]))
        return Response(json.dumps({"token": "anon"}).encode()) if isinstance(req, str) else Response()

    assert ready.pullable("ghcr.io/org/app", "v0.1.0", urlopen=public)
    assert seen == [
        "https://ghcr.io/token?scope=repository:org/app:pull&service=ghcr.io",
        ("HEAD", "https://ghcr.io/v2/org/app/manifests/v0.1.0", "Bearer anon"),
    ]

    def private(req, timeout):
        raise urllib.error.HTTPError("https://ghcr.io/token", 403, "denied", {}, None)

    assert not ready.pullable("ghcr.io/org/app", "v0.1.0", urlopen=private)

    def unknown_tag(req, timeout):
        if isinstance(req, str):
            return Response(json.dumps({"token": "anon"}).encode())
        raise urllib.error.HTTPError(req.full_url, 404, "manifest unknown", {}, None)

    assert not ready.pullable("ghcr.io/org/app", "v0.1.0", urlopen=unknown_tag)


def test_the_report_says_what_is_left_to_do():
    built = {**item(), "state": "built", "public": True}
    assert ready.markdown([built], ["alice"]).startswith("## :white_check_mark: The app is ready")

    private = {**item("org/training-environment-rstudio-x-app", "rstudio"), "state": "built", "public": False}
    text = ready.markdown([built, private], ["alice", "geoffreyweal"])
    assert text.startswith("## :warning: Built: one step left")
    assert "[make it public](https://github.com/orgs/org/packages/container/training-environment-rstudio-x-app/settings)" in text
    assert text.rstrip().endswith("@alice @geoffreyweal")

    failed = {**item(), "state": "failed", "run_url": "https://run/9"}
    text = ready.markdown([failed], [])
    assert text.startswith("## :x: Not every image was built")
    assert "[the build failed](https://run/9)" in text
