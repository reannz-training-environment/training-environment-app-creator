"""Making the GitHub App through GitHub's manifest flow, with a stand-in browser."""

from __future__ import annotations

import html
import json
import re
import threading
import urllib.error
import urllib.request

from te_app_creator import setup_app


def test_the_app_is_made_and_its_key_goes_straight_to_the_repository(monkeypatch):
    gh_calls, seen = [], {}
    monkeypatch.setattr(setup_app, "_gh", lambda *args, input=None: gh_calls.append((args, input)) or "")
    app = {"client_id": "Iv23abc", "pem": "-----BEGIN RSA PRIVATE KEY-----", "slug": "te-creator", "html_url": "https://github.com/apps/te-creator"}
    monkeypatch.setattr(setup_app, "_convert", lambda code: app if code == "c0de" else None)

    def browser(url):
        seen.setdefault("opened", []).append(url)
        if not url.startswith("http://127.0.0.1"):
            return

        def visit():
            # the form page, which would post the manifest to GitHub...
            page = urllib.request.urlopen(url).read().decode()
            seen["action"] = html.unescape(re.search(r'action="([^"]*)"', page).group(1))
            seen["manifest"] = json.loads(html.unescape(re.search(r'name="manifest" value="([^"]*)"', page).group(1)))
            state = seen["action"].split("state=")[1]
            # ...a stranger's request is turned away...
            try:
                urllib.request.urlopen(f"{url}created?state=wrong&code=evil")
            except urllib.error.HTTPError as exc:
                seen["stranger"] = exc.code
            # ...and GitHub sends the browser back with the code
            urllib.request.urlopen(f"{url}created?state={state}&code=c0de").read()

        threading.Thread(target=visit, daemon=True).start()

    monkeypatch.setattr(setup_app.webbrowser, "open", browser)
    assert setup_app.run("my-org", "creator", "Test app creator", "https://site.example", timeout=20) == 0

    assert seen["action"].startswith("https://github.com/organizations/my-org/settings/apps/new?state=")
    manifest = seen["manifest"]
    assert manifest["name"] == "Test app creator" and manifest["public"] is False
    assert re.fullmatch(r"http://127\.0\.0\.1:\d+/created", manifest["redirect_url"])
    assert manifest["default_permissions"]["administration"] == "write"
    assert manifest["hook_attributes"]["active"] is False
    assert seen["stranger"] == 404

    assert (("variable", "set", "APP_CREATOR_CLIENT_ID", "--repo", "my-org/creator", "--body", "Iv23abc"), None) in gh_calls
    # the key goes in on stdin, never on the command line
    assert (("secret", "set", "APP_CREATOR_APP_PRIVATE_KEY", "--repo", "my-org/creator"), app["pem"]) in gh_calls
    assert seen["opened"][-1] == "https://github.com/apps/te-creator/installations/new"
