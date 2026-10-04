"""App requests: from an issue made by the website, to a pull request, to approval."""

from __future__ import annotations

import base64
import itertools
import json
import re
import subprocess
from urllib.parse import parse_qs, urlparse

import pytest

from conftest import EXAMPLES
from te_app_creator import publish, request

REPO = "org/creator"
MINIMAL = [p for p in EXAMPLES if p.name == "minimal.yml"][0].read_text()


def issue_body(spec: str) -> str:
    """An issue body the way GitHub writes it for the request form."""
    return "### App spec\n\n```yaml\n" + spec.rstrip("\n") + "\n```\n"


def spec_text(name="intro-shell", title="Intro to the shell", version="0.1.0", extra=""):
    return (
        "# A REANNZ training environment app, from the app creator website.\n"
        "schema_version: 1\n"
        f"name: {name}\n"
        f"title: {title}\n"
        f"version: {version}\n"
        "interfaces:\n  - jupyter\n" + extra
    )


class FakeGitHub(publish.GitHub):
    """The parts of GitHub's REST API that requests use, in memory."""

    def __init__(self, members=("alice", "bob")):
        super().__init__("token")
        self.members = set(members)
        self.ids = itertools.count(1)
        self.commits = {"c0": {}}  # sha -> {path: text}
        self.refs = {"main": "c0"}
        self.pulls = {}  # number -> pull request
        self.issues = {}  # number -> {"state", "comments": [...]}
        self.reviewers = {}
        self.log = []

    # helpers for tests
    def put_file(self, path, text, branch="main"):
        sha = f"c{next(self.ids)}"
        self.commits[sha] = {**self.commits[self.refs[branch]], path: text}
        self.refs[branch] = sha

    def comments(self, number):
        return [c["body"] for c in self.issues.get(number, {}).get("comments", [])]

    def _tree(self, ref):
        return self.commits[self.refs.get(ref, ref)]

    def _number(self):
        return max([*self.pulls, *self.issues, 100]) + 1

    def request(self, method, path, data=None):
        self.log.append((method, path))
        url = urlparse(path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        p = url.path
        m = lambda pattern: re.fullmatch(pattern, p)  # noqa: E731

        if method == "GET" and p == f"/repos/{REPO}":
            return 200, {"default_branch": "main"}
        if r := m(rf"/repos/{REPO}/git/refs?/heads/(.+)"):
            branch = r.group(1)
            if method == "GET":
                return (200, {"object": {"sha": self.refs[branch]}}) if branch in self.refs else (404, None)
            if method == "PATCH":
                self.refs[branch] = data["sha"]
                return 200, {}
            if method == "DELETE":
                self.refs.pop(branch, None)
                return 204, None
        if method == "POST" and p == f"/repos/{REPO}/git/refs":
            self.refs[data["ref"].removeprefix("refs/heads/")] = data["sha"]
            return 201, {}
        if r := m(rf"/repos/{REPO}/contents/(.+)"):
            file = r.group(1)
            if method == "GET":
                text = self._tree(query["ref"]).get(file)
                if text is None:
                    return 404, None
                return 200, {"content": base64.b64encode(text.encode()).decode(), "sha": f"blob-{hash(text)}"}
            if method == "PUT":
                old = self._tree(data["branch"]).get(file)
                assert (old is None) == ("sha" not in data), "updating a file needs its blob sha"
                self.put_file(file, base64.b64decode(data["content"]).decode(), data["branch"])
                return 201, {}
        if p == f"/repos/{REPO}/pulls":
            if method == "GET":
                found = [pr for pr in self.pulls.values() if pr["state"] == "open"]
                if "head" in query:
                    found = [pr for pr in found if f"org:{pr['head']['ref']}" == query["head"]]
                return 200, found
            if method == "POST":
                number = self._number()
                self.pulls[number] = {
                    "number": number,
                    "state": "open",
                    "title": data["title"],
                    "body": data["body"],
                    "head": {"ref": data["head"]},
                    "user": {"login": "app-creator[bot]"},
                    "html_url": f"https://github.com/{REPO}/pull/{number}",
                }
                return 201, self.pulls[number]
        if r := m(rf"/repos/{REPO}/pulls/(\d+)"):
            pr = self.pulls[int(r.group(1))]
            if method == "GET":
                return 200, pr
            if method == "PATCH":
                pr.update(data)
                return 200, pr
        if r := m(rf"/repos/{REPO}/pulls/(\d+)/files"):
            pr = self.pulls[int(r.group(1))]
            head, base = self._tree(pr["head"]["ref"]), self._tree("main")
            return 200, [{"filename": f} for f in head if head.get(f) != base.get(f)]
        if r := m(rf"/repos/{REPO}/pulls/(\d+)/requested_reviewers"):
            self.reviewers[int(r.group(1))] = data["reviewers"]
            return 201, {}
        if r := m(rf"/repos/{REPO}/issues/(\d+)/comments"):
            issue = self.issues.setdefault(int(r.group(1)), {"state": "open", "comments": []})
            if method == "GET":
                return 200, issue["comments"]
            comment = {"id": next(self.ids), "body": data["body"]}
            issue["comments"].append(comment)
            return 201, comment
        if r := m(rf"/repos/{REPO}/issues/comments/(\d+)"):
            for issue in self.issues.values():
                for comment in issue["comments"]:
                    if comment["id"] == int(r.group(1)):
                        comment["body"] = data["body"]
                        return 200, comment
        if (r := m(rf"/repos/{REPO}/issues/(\d+)")) and method == "PATCH":
            self.issues.setdefault(int(r.group(1)), {"comments": []})["state"] = data["state"]
            return 200, {}
        if (r := m(rf"/repos/{REPO}/issues/(\d+)")) and method == "GET":
            return 200, self.issue_payloads[int(r.group(1))]
        if r := m(rf"/repos/{REPO}/collaborators/([^/]+)/permission"):
            permission = getattr(self, "permissions", {}).get(r.group(1), "read")
            return 200, {"permission": permission}
        if r := m(rf"/repos/{REPO}/collaborators/(.+)"):
            return (204, None) if r.group(1) in self.members else (404, None)
        raise AssertionError(f"unexpected call {method} {path}")


def event(number, spec, user="alice", association="MEMBER", action="opened", labels=(), label=None):
    payload = {
        "action": action,
        "issue": {
            "number": number,
            "state": "open",
            "body": issue_body(spec),
            "user": {"login": user},
            "author_association": association,
            "labels": [{"name": name} for name in labels],
        },
    }
    if label:
        payload["label"] = {"name": label}
    return payload


def handle(gh, payload, schema, defaults, **kwargs):
    return request.handle(payload, REPO, gh, schema, defaults, log=lambda m: None, **kwargs)


# ------------------------------------------------------------------ reading


def test_the_spec_is_read_from_the_form():
    spec = spec_text()
    assert request.extract_spec(issue_body(spec)) == spec
    assert request.extract_spec(issue_body(spec).replace("\n", "\r\n")) == spec


def test_a_fence_or_heading_inside_the_spec_does_not_cut_it_short():
    spec = spec_text(extra="description: |\n  ### not a heading\n  ```\n  code\n  ```\n")
    assert request.extract_spec(issue_body(spec)) == spec


@pytest.mark.parametrize("body", ["", "Please make me an app", "### App spec\n\n_No response_\n", "### App spec\n\n```yaml\n```\n"])
def test_a_request_without_a_spec_is_refused(body):
    with pytest.raises(request.RequestError):
        request.extract_spec(body)


def test_versions():
    assert request.next_version("0.1.9") == "0.1.10"
    assert request.set_version(spec_text(), "0.1.1") == spec_text(version="0.1.1")
    no_version = spec_text().replace("version: 0.1.0\n", "")
    assert "name: intro-shell\nversion: 0.2.0\n" in request.set_version(no_version, "0.2.0")


def test_a_new_app_is_planned_as_it_is(schema, defaults):
    text = spec_text()
    plan = request.plan(text, request.parse(text), None, schema, defaults)
    assert (plan.name, plan.version, plan.previous, plan.bumped) == ("intro-shell", "0.1.0", None, False)
    assert plan.text == text and plan.path == "apps/intro-shell.yml"


def test_a_change_to_an_existing_app_gets_a_new_version(schema, defaults):
    old = spec_text(version="0.1.3")
    new = spec_text(title="Intro to the Unix shell")
    plan = request.plan(new, request.parse(new), old, schema, defaults)
    assert (plan.previous, plan.version, plan.bumped) == ("0.1.3", "0.1.4", True)
    assert "version: 0.1.4\n" in plan.text and plan.text.startswith("# A REANNZ")

    # a version the requester raised themselves is kept
    newer = spec_text(title="Intro to the Unix shell", version="1.0.0")
    plan = request.plan(newer, request.parse(newer), old, schema, defaults)
    assert (plan.version, plan.bumped) == ("1.0.0", False)


def test_asking_for_what_is_already_there_changes_nothing(schema, defaults):
    text = spec_text(version="0.1.0")
    with pytest.raises(request.NothingToDo):
        request.plan(text, request.parse(text), spec_text(version="0.1.2"), schema, defaults)


def test_problems_with_the_spec_are_listed(schema, defaults):
    text = spec_text().replace("  - jupyter\n", "  - matlab\n")
    with pytest.raises(request.RequestError, match="interfaces/0"):
        request.plan(text, request.parse(text), None, schema, defaults)
    with pytest.raises(request.RequestError, match="lower case"):
        request.parse(spec_text(name="Intro_Shell"))
    with pytest.raises(request.RequestError, match="not valid YAML"):
        request.parse("name: [unclosed\n")


def test_the_summary_warns_of_a_change_that_would_not_be_released(tmp_path, monkeypatch, schema, defaults):
    from te_app_creator import cli
    from te_app_creator.spec import load

    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True, text=True).stdout

    path = tmp_path / "apps" / "intro-shell.yml"
    path.parent.mkdir()
    path.write_text(spec_text())
    git("init", "-q")
    git("add", "-A")
    git("-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-q", "-m", "base")
    base = git("rev-parse", "HEAD").strip()
    monkeypatch.setattr(cli, "ROOT", tmp_path)

    path.write_text(spec_text(title="Intro to the Unix shell"))
    warning = cli._unreleased_change(load(path, schema, defaults), base)
    assert "without raising its version (still v0.1.0)" in warning and "Make the version 0.1.1" in warning

    path.write_text(spec_text(title="Intro to the Unix shell", version="0.1.1"))
    assert cli._unreleased_change(load(path, schema, defaults), base) == ""

    other = tmp_path / "apps" / "new-app.yml"
    other.write_text(spec_text(name="new-app"))
    assert cli._unreleased_change(load(other, schema, defaults), base) == ""


def test_custom_commands_are_flagged_for_the_reviewer(schema, defaults):
    text = spec_text(extra="advanced:\n  dockerfile: RUN echo hello\n")
    plan = request.plan(text, request.parse(text), None, schema, defaults)
    assert plan.review == ["it adds its own Dockerfile instructions (`advanced.dockerfile`)"]
    assert ":warning: **Read the spec before approving:**" in request.pr_body(plan, "alice", 3, defaults, [])


# ---------------------------------------------------------------- handling


def test_a_request_becomes_a_pull_request(schema, defaults):
    gh = FakeGitHub()
    outcome = handle(gh, event(7, spec_text()), schema, defaults)
    assert outcome.startswith("pull request")

    assert gh._tree("app-request/7")["apps/intro-shell.yml"] == spec_text()
    (pr,) = gh.pulls.values()
    assert pr["title"] == "Add app: Intro to the shell (intro-shell)"
    assert pr["head"]["ref"] == "app-request/7"
    assert pr["body"].startswith("Requested by @alice in #7")
    assert "training-environment-jupyter-intro-shell-app" in pr["body"]
    assert pr["body"].rstrip().endswith("Closes #7")

    (status,) = gh.comments(7)
    assert status.startswith(request.MARKER)
    assert f"this request is pull request #{pr['number']}" in status

    # the same request again: same branch, same pull request, no new commit
    commit = gh.refs["app-request/7"]
    handle(gh, event(7, spec_text(), action="edited"), schema, defaults)
    assert gh.refs["app-request/7"] == commit
    assert len(gh.pulls) == 1 and len(gh.comments(7)) == 1


def test_an_edited_request_starts_again_from_main(schema, defaults):
    gh = FakeGitHub()
    handle(gh, event(7, spec_text()), schema, defaults)
    handle(gh, event(7, spec_text(name="intro-bash"), action="edited"), schema, defaults)
    tree = gh._tree("app-request/7")
    assert "apps/intro-bash.yml" in tree and "apps/intro-shell.yml" not in tree
    (pr,) = gh.pulls.values()
    assert "(intro-bash)" in pr["title"]


def test_a_change_to_an_existing_app_raises_its_version(schema, defaults):
    gh = FakeGitHub()
    gh.put_file("apps/intro-shell.yml", spec_text(version="0.2.0"))
    handle(gh, event(8, spec_text(title="Intro to the Unix shell")), schema, defaults)
    assert "version: 0.2.1\n" in gh._tree("app-request/8")["apps/intro-shell.yml"]
    (pr,) = gh.pulls.values()
    assert pr["title"] == "Update app: Intro to the Unix shell (intro-shell)"
    assert "from v0.2.0 to v0.2.1" in pr["body"]
    assert "did not raise the version" in pr["body"]


def test_a_request_with_nothing_new_opens_nothing(schema, defaults):
    gh = FakeGitHub()
    gh.put_file("apps/intro-shell.yml", spec_text())
    outcome = handle(gh, event(9, spec_text()), schema, defaults)
    assert "nothing to change" in outcome
    assert not gh.pulls and "app-request/9" not in gh.refs
    assert "nothing to change" in gh.comments(9)[0]


def test_a_broken_request_is_explained_to_the_requester(schema, defaults):
    gh = FakeGitHub()
    outcome = handle(gh, event(10, spec_text().replace("  - jupyter\n", "  - matlab\n")), schema, defaults)
    assert outcome.startswith("not opened")
    assert not gh.pulls
    (comment,) = gh.comments(10)
    assert "@alice: this request cannot be built yet" in comment and "interfaces/0" in comment


def test_outsiders_wait_for_a_maintainer(schema, defaults):
    gh = FakeGitHub()
    outsider = event(11, spec_text(), user="mallory", association="NONE")
    assert handle(gh, outsider, schema, defaults) == "waiting for a maintainer to approve the request"
    assert not gh.pulls and "request approved" in gh.comments(11)[0]

    # other labels change nothing; the approval label goes ahead
    other = event(11, spec_text(), user="mallory", association="NONE", action="labeled", labels=["question"], label="question")
    assert handle(gh, other, schema, defaults) == "only the approval label matters"
    approved = event(
        11, spec_text(), user="mallory", association="NONE", action="labeled",
        labels=["app request", "request approved"], label="request approved",
    )
    assert handle(gh, approved, schema, defaults).startswith("pull request")
    assert len(gh.pulls) == 1


def test_collaborators_whose_membership_github_hides_are_trusted(schema, defaults):
    gh = FakeGitHub(members=("carol",))
    assert handle(gh, event(12, spec_text(), user="carol", association="CONTRIBUTOR"), schema, defaults).startswith("pull request")


class Org(publish.GitHub):
    """What a token that can read the organisation's members sees."""

    def __init__(self, members):
        super().__init__("members-token")
        self.members = members

    def request(self, method, path, data=None):
        login = path.rsplit("/", 1)[1]
        assert path == f"/orgs/org/members/{login}"
        return (204, None) if login in self.members else (404, None)


def test_private_members_reviewers_and_writers_are_trusted(schema, defaults):
    # GitHub reports a private member as a CONTRIBUTOR, and the App cannot
    # see them as a collaborator without the Members permission
    private = event(13, spec_text(), user="dana", association="CONTRIBUTOR")
    gh = FakeGitHub(members=())
    assert handle(gh, private, schema, defaults).startswith("waiting")
    assert "cannot tell that you are in the organisation" in gh.comments(13)[0]
    gh = FakeGitHub(members=())
    assert handle(gh, private, schema, defaults, members=Org({"dana"})).startswith("pull request")

    # the reviewers are trusted from the config alone
    reviewer = event(14, spec_text(), user="GeoffreyWeal", association="CONTRIBUTOR")
    assert handle(FakeGitHub(members=()), reviewer, schema, {**defaults, "reviewers": ["geoffreyweal"]}).startswith("pull request")

    # and so is anyone who can write to the repository
    gh = FakeGitHub(members=())
    gh.permissions = {"erin": "write", "frank": "read"}
    assert handle(gh, event(15, spec_text(), user="erin", association="NONE"), schema, defaults).startswith("pull request")
    assert handle(gh, event(16, spec_text(name="other"), user="frank", association="NONE"), schema, defaults).startswith("waiting")


def test_a_maintainer_can_send_any_request_through(schema, defaults):
    gh = FakeGitHub(members=())
    retried = {**event(17, spec_text(), user="mallory", association="NONE"), "action": "retried"}
    assert handle(gh, retried, schema, defaults, approved=True).startswith("pull request")


def test_a_request_can_be_tried_again_by_its_number(monkeypatch):
    from te_app_creator import cli

    gh = FakeGitHub(members=())
    gh.issue_payloads = {51: event(51, spec_text(), user="mallory", association="NONE")["issue"]}
    monkeypatch.setattr(cli.publisher, "GitHub", lambda token: gh)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("APP_CREATOR_TOKEN", "app-token")
    monkeypatch.delenv("MEMBERS_TOKEN", raising=False)
    assert cli.main(["request", "--issue", "51", "--approved"]) == 0
    assert [pr["head"]["ref"] for pr in gh.pulls.values()] == ["app-request/51"]


def test_without_the_app_the_requester_is_told_how_it_gets_picked_up(monkeypatch, tmp_path):
    from te_app_creator import cli

    gh = FakeGitHub()
    monkeypatch.setattr(cli.publisher, "GitHub", lambda token: gh)
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event(50, spec_text())))
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("GH_TOKEN", "actions-token")
    monkeypatch.delenv("APP_CREATOR_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    assert cli.main(["request", "--event", str(path)]) == 1
    (comment,) = gh.comments(50)
    assert "cannot act on requests yet" in comment
    assert f"re-running [this run](https://github.com/{REPO}/actions/runs/123) picks this request up" in comment
    assert not gh.pulls


def test_asking_again_replaces_your_earlier_request(schema, defaults):
    gh = FakeGitHub()
    handle(gh, event(20, spec_text()), schema, defaults)
    (first,) = gh.pulls
    handle(gh, event(21, spec_text(title="Intro to the shell, take two")), schema, defaults)
    second = max(gh.pulls)
    assert gh.pulls[first]["state"] == "closed"
    assert "app-request/20" not in gh.refs
    assert gh.issues[20]["state"] == "closed"
    assert f"Replaced by #21" in gh.comments(20)[-1]
    assert gh.pulls[second]["state"] == "open"


def test_someone_elses_request_for_the_same_app_is_left_open(schema, defaults):
    gh = FakeGitHub()
    handle(gh, event(30, spec_text()), schema, defaults)
    (first,) = gh.pulls
    handle(gh, event(31, spec_text(title="Shell basics"), user="bob"), schema, defaults)
    second = max(gh.pulls)
    assert gh.pulls[first]["state"] == "open"
    assert f"Another open request, #{first}, also changes `apps/intro-shell.yml`." in gh.pulls[second]["body"]


# ----------------------------------------------------------------- handoff


def test_built_requests_go_to_the_reviewers(schema, defaults):
    gh = FakeGitHub()
    handle(gh, event(40, spec_text()), schema, defaults)
    (number,) = gh.pulls
    defaults = {**defaults, "reviewers": ["geoffreyweal"]}
    assert request.handoff(gh, REPO, number, "success", "https://run", defaults) == "asked for approval"
    assert gh.reviewers[number] == ["geoffreyweal"]
    (status,) = gh.comments(40)
    assert "- [x] Every image test-builds" in status
    assert "@geoffreyweal has been asked to review it" in status


def test_a_failed_build_is_reported_to_the_requester(schema, defaults):
    gh = FakeGitHub()
    handle(gh, event(41, spec_text()), schema, defaults)
    (number,) = gh.pulls
    request.handoff(gh, REPO, number, "failure", "https://run/1", defaults)
    assert number not in gh.reviewers
    status, news = gh.comments(41)
    assert ":x: An image did not build: see [the checks](https://run/1)" in status
    assert news.startswith("@alice: an image of this app did not build")


def test_handoff_ignores_other_pull_requests_and_cancelled_runs(schema, defaults):
    gh = FakeGitHub()
    gh.pulls[5] = {"number": 5, "state": "open", "body": "A template change", "head": {"ref": "fix-things"}, "user": {"login": "bob"}}
    assert request.handoff(gh, REPO, 5, "success", "", defaults) == "not an open app request"
    handle(gh, event(42, spec_text()), schema, defaults)
    number = max(gh.pulls)
    assert "nothing to say" in request.handoff(gh, REPO, number, "cancelled", "", defaults)
    assert len(gh.comments(42)) == 1
