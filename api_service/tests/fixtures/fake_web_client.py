"""
A fake JD Edwards web client over real HTTPS on 127.0.0.1 -- TEST FIXTURE
ONLY. It stands in for the customer's web client at the network boundary so
the browser executor (executors/browser.py) runs for real: Chromium, the
host guard, the certificate pin, Jade's own sign-in, the agent's tools and
the screenshots.

Pages: a sign-in form (User, Password, Environment, Role), a version's
processing options (saved into the same DEV state the fake AIS server reads,
so the read-back is a live AIS read), and a batch version's data selection.
Each page also shows a Delete button the executor must never press.
"""

from __future__ import annotations

import html
import http.server
import ssl
import threading
import urllib.parse
from typing import Optional

from . import ais_estate


class FakeWebClient:
    def __init__(self, cert: str, key: str, *, company: str = "vdb", environment: str = "JDV920",
                 username: str = "JADEWRITE", password: str = "") -> None:
        self.company, self.environment = company, environment
        self.username, self.password = username, password
        self.data_selection: dict[str, str] = {}
        self.sign_ins: list[dict] = []
        self.deleted = False
        self.requests: list[str] = []
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _page(self, body: str, status: int = 200, cookie: Optional[str] = None) -> None:
                data = f"<!doctype html><html><head><title>JD Edwards</title></head><body>{body}</body></html>".encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                if cookie:
                    self.send_header("Set-Cookie", cookie)
                self.end_headers()
                self.wfile.write(data)

            def _signed_in(self) -> bool:
                return "jde_session=ok" in (self.headers.get("Cookie") or "")

            def do_GET(self):
                fixture.requests.append("GET " + self.path)
                url = urllib.parse.urlparse(self.path)
                q = dict(urllib.parse.parse_qsl(url.query))
                if url.path in ("/jde", "/jde/"):
                    if self._signed_in():
                        return self._page("<h1>Welcome</h1><p>Fast path: open /jde/po or /jde/ds</p>")
                    return self._page(
                        '<form method="post" action="/jde/login"><label for="User">User</label>'
                        '<input id="User" name="User"><label for="Password">Password</label>'
                        '<input id="Password" name="Password" type="password">'
                        '<input name="Environment" id="Environment"><input name="Role" id="Role">'
                        '<button type="submit">Sign In</button></form>')
                if not self._signed_in():
                    return self._page("<p>Please sign in</p>", 401)
                if url.path == "/jde/po":
                    app, ver = q.get("app", ""), q.get("ver", "")
                    values = ais_estate.load(fixture.company, fixture.environment)["processing_options"].get(
                        f"{app}|{ver}") or {}
                    rows = "".join(f'<label for="po_{k}">{html.escape(k)}</label><input id="po_{k}" name="{k}" '
                                   f'value="{html.escape(str(v))}"><br>' for k, v in sorted(values.items()))
                    return self._page(f'<h1>Processing Options {html.escape(app)} {html.escape(ver)}</h1>'
                                      f'<form method="post" action="/jde/po?app={app}&ver={ver}">{rows}'
                                      '<button type="submit" id="ok">OK</button></form>'
                                      '<form method="post" action="/jde/delete"><button id="del">Delete</button></form>')
                if url.path == "/jde/ds":
                    key = f"{q.get('app', '')}|{q.get('ver', '')}"
                    spec = fixture.data_selection.get(key, "")
                    return self._page(f'<h1>Data Selection {html.escape(key)}</h1>'
                                      f'<form method="post" action="/jde/ds?app={q.get("app")}&ver={q.get("ver")}">'
                                      f'<label for="spec">Data Selection</label><textarea id="spec" name="spec">'
                                      f'{html.escape(spec)}</textarea><button type="submit" id="ok">OK</button></form>'
                                      '<form method="post" action="/jde/delete"><button id="del">Delete</button></form>')
                return self._page("<p>not found</p>", 404)

            def do_POST(self):
                fixture.requests.append("POST " + self.path)
                url = urllib.parse.urlparse(self.path)
                q = dict(urllib.parse.parse_qsl(url.query))
                form = dict(urllib.parse.parse_qsl(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()))
                if url.path == "/jde/login":
                    fixture.sign_ins.append({k: v for k, v in form.items() if k != "Password"})
                    if form.get("User") != fixture.username or form.get("Password") != fixture.password:
                        return self.do_GET_login_failed()
                    self.send_response(303)
                    self.send_header("Location", "/jde/")
                    self.send_header("Set-Cookie", "jde_session=ok; Path=/; Secure")
                    self.end_headers()
                    return None
                if not self._signed_in():
                    return self._page("<p>Please sign in</p>", 401)
                if url.path == "/jde/delete":
                    fixture.deleted = True
                    return self._page("<p>deleted</p>")
                if url.path == "/jde/po":
                    app, ver = q.get("app", ""), q.get("ver", "")
                    with ais_estate.edit(fixture.company, fixture.environment, actor="JD Edwards web client",
                                         reason=f"processing options {app}|{ver} saved") as e:
                        for k, v in form.items():
                            ais_estate.set_processing_option(e, app, ver, k, v)
                    return self._page("<p>Processing options saved.</p>")
                if url.path == "/jde/ds":
                    fixture.data_selection[f"{q.get('app')}|{q.get('ver')}"] = form.get("spec", "")
                    return self._page("<p>Data selection saved.</p>")
                return self._page("<p>not found</p>", 404)

            def do_GET_login_failed(self):
                return self._page('<p>Invalid user or password</p><form method="post" action="/jde/login">'
                                  '<input id="User" name="User"><input id="Password" name="Password" type="password">'
                                  '<button type="submit">Sign In</button></form>')

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"https://127.0.0.1:{self.server.server_address[1]}/jde"

    def close(self) -> None:
        self.server.shutdown()
