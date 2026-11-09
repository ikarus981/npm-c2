#!/usr/bin/env python3
"""Minimal in-memory npm dist-tags registry for offline testing.

Implements the three routes npm-c2 touches:
  GET    /-/package/<pkg>/dist-tags
  PUT    /-/package/<pkg>/dist-tags/<tag>   body: JSON string
  DELETE /-/package/<pkg>/dist-tags/<tag>
"""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_DIST = re.compile(r"^/-/package/(.+?)/dist-tags(?:/(.+))?$")


class Registry:
    def __init__(self):
        self.lock = threading.Lock()
        self.tags = {}          # pkg -> {tag: version}
        self.writes = []        # (method, pkg, tag)
        self.gets = 0

    def snapshot(self, pkg):
        with self.lock:
            return dict(self.tags.get(pkg, {}))


def make_handler(reg: Registry, auth_token=None):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _pkg_tag(self):
            m = _DIST.match(self.path.split("?")[0])
            if not m:
                return None, None
            return m.group(1), m.group(2)

        def _auth_ok(self):
            if auth_token is None:
                return True
            return self.headers.get("Authorization") == f"Bearer {auth_token}"

        def _json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            pkg, tag = self._pkg_tag()
            if pkg is None:
                return self._json(404, {"error": "not found"})
            with reg.lock:
                reg.gets += 1
                known = pkg in reg.tags
                tags = dict(reg.tags.get(pkg, {}))
            if tag:
                if tag in tags:
                    return self._json(200, tags[tag])
                return self._json(404, {"error": "tag not found"})
            if not known:
                return self._json(404, {"error": "package not found"})
            return self._json(200, tags)

        def do_PUT(self):
            pkg, tag = self._pkg_tag()
            if not pkg or not tag:
                return self._json(404, {"error": "not found"})
            if not self._auth_ok():
                return self._json(401, {"error": "unauthorized"})
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n)
            try:
                value = json.loads(raw)
            except Exception:
                return self._json(400, {"error": "bad json"})
            with reg.lock:
                reg.tags.setdefault(pkg, {})[tag] = value
                reg.writes.append(("PUT", pkg, tag))
            return self._json(200, {"ok": True})

        def do_DELETE(self):
            pkg, tag = self._pkg_tag()
            if not pkg or not tag:
                return self._json(404, {"error": "not found"})
            if not self._auth_ok():
                return self._json(401, {"error": "unauthorized"})
            with reg.lock:
                existed = reg.tags.get(pkg, {}).pop(tag, None) is not None
                reg.writes.append(("DELETE", pkg, tag))
            return self._json(200 if existed else 404, {"ok": existed})

    return H


def serve(reg: Registry, auth_token=None, port=0):
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(reg, auth_token))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"
