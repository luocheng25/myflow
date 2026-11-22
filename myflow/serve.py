"""`mf serve`: a tiny local web UI (stdlib only). Always renders fresh from events.

Routes: /  (all tasks) · /t/<id>/ (task) · /t/<id>/<file> (L3 files; .md rendered, ?raw=1 for the source) · POST /t/<id>/steer|decide · /api/status
Binds 127.0.0.1 by default; VS Code Remote forwards the port automatically.
"""

from __future__ import annotations

import json
import mimetypes
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from myflow import render
from myflow.core import MfError, State, all_task_dirs, append_event, file_hash, task_dir_of

TEXT_EXT = {".log", ".jsonl", ".json", ".md", ".patch", ".diff", ".txt", ".py", ".sh", ".yaml", ".yml", ".csv", ""}


class Handler(BaseHTTPRequestHandler):
    server_version = "myflow"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, to: str):
        self.send_response(303)
        self.send_header("Location", to)
        self.end_headers()

    def _task(self, tid: str) -> State:
        return State(task_dir_of(tid))

    def do_GET(self):
        try:
            path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
            if path in ("/", "/index.html"):
                return self._send(200, render.html_index([State(d) for d in all_task_dirs()], served=True))
            if path == "/api/status":
                rows = [render.summary(State(d)) for d in all_task_dirs()]
                return self._send(200, json.dumps(rows, ensure_ascii=False, default=str), "application/json")
            parts = path.split("/", 3)
            if len(parts) >= 3 and parts[1] == "t":
                st = self._task(parts[2])
                rest = parts[3] if len(parts) > 3 else ""
                if rest in ("", "dashboard.html"):
                    if not path.endswith("/") and rest == "":
                        return self._redirect(path + "/")
                    return self._send(200, render.html_task(st, served=True))
                f = (st.dir / rest).resolve()
                if st.dir.resolve() not in f.parents or not f.is_file():
                    return self._send(404, "not found", "text/plain")
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if f.suffix.lower() in (".md", ".markdown") and "raw" not in query:
                    text = f.read_text(encoding="utf-8", errors="replace")
                    nav = (f" · <a href='/'>全部任务</a> · <a href='/t/{urllib.parse.quote(st.id)}/'>{render.esc(st.id)}</a> · "
                           f"{render.esc(rest)} · <a href='?raw=1'>原文</a>")
                    return self._send(200, render._page(f"{f.name} · myflow", f"<div class='card'>{render.md_html(text)}</div>", nav))
                ctype = "text/plain; charset=utf-8" if f.suffix in TEXT_EXT else (mimetypes.guess_type(f.name)[0] or "application/octet-stream")
                return self._send(200, f.read_bytes(), ctype)
            return self._send(404, "not found", "text/plain")
        except MfError as e:
            return self._send(404, str(e), "text/plain; charset=utf-8")

    def do_POST(self):
        try:
            parts = urllib.parse.urlparse(self.path).path.split("/")
            if len(parts) != 4 or parts[1] != "t" or parts[3] not in ("steer", "decide"):
                return self._send(404, "not found", "text/plain")
            tdir = task_dir_of(urllib.parse.unquote(parts[2]))
            n = int(self.headers.get("Content-Length") or 0)
            form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8")).items()}
            text = (form.get("text") or "").strip()
            if parts[3] == "steer" and text:
                append_event(tdir, "steer", text[:2000], lane=(form.get("lane") or "").strip() or None, by="user",
                             counter="steer", via="web")
            elif parts[3] == "decide" and form.get("gate"):
                gate = form["gate"]
                extra = {}
                if gate == "spec":
                    from myflow import ms
                    extra = {"spec_hash": file_hash(tdir / "SPEC.md"), "push_key": ms.spec_push_key(State(tdir))}
                append_event(tdir, "decide", text or "批准（网页）", by="user", gate=gate, via="web", **extra)
            try:
                render.render_task(State(tdir))
            except Exception as e:  # rendering must never block recording the user's input
                print(f"[mf serve] render failed: {e}", file=sys.stderr)
            return self._redirect(f"/t/{parts[2]}/")
        except MfError as e:
            return self._send(400, str(e), "text/plain; charset=utf-8")


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"myflow dashboard: http://{host}:{port}/   (Ctrl-C to stop; in VS Code: 'Simple Browser: Show')", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
