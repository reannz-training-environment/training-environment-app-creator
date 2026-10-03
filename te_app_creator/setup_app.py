"""Make the app creator's GitHub App, and hand its credentials to the workflows.

Run it once, as an owner of the organisation, with the GitHub CLI (gh) signed
in:

    python -m te_app_creator setup-app

It uses GitHub's manifest flow. A page on this computer sends the App's
settings to GitHub, where you press "Create GitHub App". GitHub hands the App's
private key back to this program, which puts it straight into the repository's
Actions secrets (it is never written to disk), and its client ID into a
variable. Then GitHub's page for installing the App opens: install it on all
the organisation's repositories, so it can manage the ones it creates.
"""

from __future__ import annotations

import html
import json
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

# everything the workflows do with the App's token, and nothing more
PERMISSIONS = {
    "administration": "write",  # create the app repositories
    "contents": "write",  # push to them, and commit requests here
    "workflows": "write",  # what it pushes includes GitHub workflows
    "pull_requests": "write",  # open request and update pull requests, merge the update ones
    "issues": "write",  # answer app requests
    "metadata": "read",
}

CLIENT_ID_VARIABLE = "APP_CREATOR_CLIENT_ID"
KEY_SECRET = "APP_CREATOR_APP_PRIVATE_KEY"


def manifest(org: str, repo: str, name: str, redirect_url: str, website: str) -> dict:
    return {
        "name": name,
        "url": f"https://github.com/{org}/{repo}",
        "description": (
            "Turns app requests from the app creator website into pull requests, and makes and "
            "updates the training environment app repositories once a request is approved."
        ),
        "public": False,
        "redirect_url": redirect_url,
        "hook_attributes": {"url": website or f"https://github.com/{org}/{repo}", "active": False},
        "default_permissions": PERMISSIONS,
        "default_events": [],
    }


def _form(org: str, manifest_json: str, state: str) -> str:
    action = f"https://github.com/organizations/{org}/settings/apps/new?state={state}"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>App creator setup</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 40rem; margin: 3rem auto; padding: 0 1rem">
<h1>Setting up the app creator</h1>
<p>Taking you to GitHub to create the app creator's GitHub App for <strong>{html.escape(org)}</strong>.
There, check the name and press <strong>Create GitHub App</strong>.</p>
<form id="f" method="post" action="{html.escape(action)}">
<input type="hidden" name="manifest" value="{html.escape(manifest_json)}">
<button type="submit">Continue to GitHub</button>
</form>
<script>document.getElementById("f").submit();</script>
</body></html>"""


DONE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>App creator setup</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 40rem; margin: 3rem auto; padding: 0 1rem">
<h1>The GitHub App is created</h1>
<p>Its credentials are going into the repository now; the terminal says when that is done.
GitHub's page for installing it opens next.</p>
</body></html>"""


def _gh(*args: str, input: str | None = None) -> str:
    proc = subprocess.run(["gh", *args], input=input, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"gh {args[0]} {args[1] if len(args) > 1 else ''} failed: {proc.stderr.strip()}")
    return proc.stdout


def _convert(code: str) -> dict:
    """Swap the code GitHub sent back for the App's ID, client ID and private key."""
    url = f"https://api.github.com/app-manifests/{code}/conversions"
    attempts = [None]
    try:
        attempts.append(_gh("auth", "token").strip())
    except SystemExit:
        pass
    last = None
    for token in attempts:
        req = urllib.request.Request(url, data=b"", method="POST")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}: {exc.read().decode(errors='replace')}"
    raise SystemExit(f"GitHub did not hand over the App's credentials: {last}")


def run(org: str, repo: str, name: str, website: str, timeout: float = 900) -> int:
    _gh("auth", "status")
    state = secrets.token_urlsafe(16)
    received: dict[str, str] = {}
    page: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the terminal for what matters
            pass

        def _send(self, status: int, body: str) -> None:
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if url.path == "/":
                self._send(200, page["form"])
            elif url.path == "/created" and query.get("state", [""])[0] == state and query.get("code"):
                received["code"] = query["code"][0]
                self._send(200, DONE)
            else:
                self._send(404, "Not found")

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 1
    port = server.server_address[1]
    spec = manifest(org, repo, name, f"http://127.0.0.1:{port}/created", website)
    page["form"] = _form(org, json.dumps(spec), state)
    start_url = f"http://127.0.0.1:{port}/"
    print(f"Opening {start_url} to create the GitHub App on GitHub. If no browser opens, open it yourself.")
    webbrowser.open(start_url)

    started = time.monotonic()
    while "code" not in received and time.monotonic() - started < timeout:
        server.handle_request()
    server.server_close()
    if "code" not in received:
        print("Gave up waiting for GitHub: run setup-app again to start over.", file=sys.stderr)
        return 1

    app = _convert(received["code"])
    full = f"{org}/{repo}"
    _gh("variable", "set", CLIENT_ID_VARIABLE, "--repo", full, "--body", app["client_id"])
    _gh("secret", "set", KEY_SECRET, "--repo", full, input=app["pem"])
    print(f"Created the GitHub App {app['slug']} ({app['html_url']}).")
    print(f"Stored its client ID in the {CLIENT_ID_VARIABLE} variable and its private key in the {KEY_SECRET} secret of {full}.")

    install = f"{app['html_url']}/installations/new"
    print(
        f"\nLast step: install it. Opening {install}\n"
        f"Choose {org}, then 'All repositories', so it can manage the repositories it creates, and press Install."
    )
    webbrowser.open(install)
    return 0
