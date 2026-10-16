"""HTTP server: JSON API endpoints and the single-page explorer UI."""

import base64
import hmac
import json
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from ..config import CONFIG, parse_search_seconds
from .frontend import _index_html
from .provenance import q_provenance
from .queries import EDGE_TYPES, _connect_ro, q_dataset, q_neighbors, q_search
from .routing import _route_policy, q_path, q_selected_path


def _query_policy(qs):
    policy = {}
    for key in ("allowed_languages", "allowed_edge_types", "ambiguity_guard"):
        if key not in qs:
            continue
        if len(qs[key]) != 1:
            raise ValueError(f"Supply {key} once")
        value = qs[key][0]
        if key == "ambiguity_guard":
            if value not in ("true", "false"):
                raise ValueError("ambiguity_guard must be true or false")
            policy[key] = value == "true"
        else:
            policy[key] = value.split(",")
    return policy


class Handler(BaseHTTPRequestHandler):
    db_path = None
    xml_path = None
    password = None          # None => auth disabled (local/dev)
    low_mem = False
    rmst_slots = threading.BoundedSemaphore(1)

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json", extra_headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _unauthorized(self):
        self._send(401, {"error": "unauthorized"},
                   extra_headers={"WWW-Authenticate": 'Basic realm="WikiHopper"'})

    def _authorized(self) -> bool:
        """If a password is configured, require HTTP Basic Auth (username ignored)."""
        if not self.password:
            return True
        hdr = self.headers.get("Authorization", "")
        if not hdr.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(hdr[6:].strip()).decode("utf-8")
        except Exception:
            return False
        # "user:password" — username is ignored; only the password must match
        pw = decoded.split(":", 1)[1] if ":" in decoded else decoded
        try:
            return hmac.compare_digest(pw, self.password)
        except (TypeError, ValueError):
            return False

    def do_POST(self):
        if not self._authorized():
            self._unauthorized()
            return
        path = urlparse(self.path).path
        selected_path = path == "/api/path/test"
        if not selected_path and (path not in ("/api/rmst", "/api/rmst/resolve")
                or not CONFIG["explorer"].get("rmst_enabled", False)):
            self._send(404, {"error": "not found"})
            return
        if self.headers.get_content_type() != "application/json":
            self._send(415, {"error": "Send an application/json request"})
            return
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            self._send(403, {"error": "Use the explorer's own origin"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                self._send(413, {"error": "Request must be between 1 byte and 64 KiB"})
                return
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeError):
            self._send(400, {"error": "Invalid JSON request"})
            return
        if selected_path:
            con = None
            policy = None
            try:
                if not isinstance(payload, dict):
                    raise ValueError("Send an object with an ordered node_ids list")
                con = _connect_ro(self.db_path, low_mem=self.low_mem)
                if "policy" in payload and payload["policy"] is None:
                    raise ValueError("policy must be an object")
                policy = _route_policy(con, payload.get("policy"))
                self._send(200, q_selected_path(con, payload.get("node_ids"), policy=policy))
            except (ValueError, TypeError) as error:
                self._send(400, {"error": str(error),
                                 "effective_policy": policy.as_dict() if policy else None})
            except sqlite3.Error:
                self._send(500, {"error": "The selected path could not be read"})
            finally:
                if con is not None:
                    con.close()
            return
        if not self.rmst_slots.acquire(blocking=False):
            self._send(429, {"error": "Another R-MST calculation is running; try again shortly"})
            return
        con = None
        policy = None
        try:
            from . import graph_api, rmst
            con = _connect_ro(self.db_path, low_mem=self.low_mem)
            if path == "/api/rmst":
                if not isinstance(payload, dict):
                    raise ValueError("Input must be a JSON object")
                if "policy" in payload and payload["policy"] is None:
                    raise ValueError("policy must be an object")
                policy = _route_policy(con, payload.get("policy"))
                payload = {**payload, "policy": policy}
            options = {"api": graph_api}
            result = (rmst.resolve(con, payload, **options) if path.endswith("/resolve") else
                      rmst.evaluate(con, payload, low_mem=self.low_mem, **options))
            self._send(200, result)
        except (ValueError, TypeError) as error:
            self._send(400, {"error": str(error),
                             "effective_policy": policy.as_dict() if policy else None})
        except sqlite3.Error:
            self._send(500, {"error": "The graph could not be read for this calculation"})
        finally:
            if con is not None:
                con.close()
            self.rmst_slots.release()

    def do_GET(self):
        if not self._authorized():
            self._unauthorized()
            return

        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query, keep_blank_values=True)
        low = self.low_mem

        if path == "/" or path == "/index.html":
            self._send(200, _index_html(low_mem=low), "text/html")
            return

        if path == "/api/dataset":
            con = _connect_ro(self.db_path, low_mem=low)
            try:
                self._send(200, q_dataset(con))
            finally:
                con.close()
            return

        if path == "/api/search":
            con = _connect_ro(self.db_path, low_mem=low)
            try:
                q = qs.get("q", [""])[0]
                language = qs.get("language", [""])[0] or None
                self._send(200, q_search(con, q, language=language))
            finally:
                con.close()
            return

        if path == "/api/neighbors":
            con = _connect_ro(self.db_path, low_mem=low)
            try:
                nid = int(qs.get("id", ["-1"])[0])
                limit = max(1, min(1000, int(qs.get("limit", ["120"])[0])))
                direction = qs.get("dir", ["both"])[0]
                types = qs.get("types", [""])[0]
                types = [t for t in types.split(",") if t in EDGE_TYPES]
                res = q_neighbors(con, nid, limit, types, direction)
                if res is None:
                    self._send(404, {"error": "node not found"})
                else:
                    self._send(200, res)
            except Exception as e:
                self._send(400, {"error": str(e)})
            finally:
                con.close()
            return

        if path == "/api/provenance":
            con = _connect_ro(self.db_path, low_mem=low)
            try:
                src = int(qs.get("src", ["-1"])[0])
                dst = int(qs.get("dst", ["-1"])[0])
                etype = qs.get("type", [""])[0]
                if etype not in EDGE_TYPES:
                    self._send(400, {"error": "bad edge type"})
                else:
                    self._send(200, q_provenance(con, self.xml_path, src, dst, etype))
            except Exception as e:
                self._send(400, {"error": str(e)})
            finally:
                con.close()
            return

        if path == "/api/path":
            con = _connect_ro(self.db_path, low_mem=low)
            policy = None
            try:
                policy = _route_policy(con, _query_policy(qs))
                src = int(qs.get("src", ["-1"])[0])
                dst = int(qs.get("dst", ["-1"])[0])
                search = CONFIG["search_low_memory" if low else "search"]
                default_hub = search["hub_max"]
                hub = max(100, min(200000, int(qs.get("hub", [str(default_hub)])[0])))
                default_seconds = search["time_budget"]
                seconds = parse_search_seconds(qs.get("seconds", [str(default_seconds)])[0])
                full_graph = qs.get("exhaustive", ["0"])[0]
                if full_graph not in ("0", "1"):
                    raise ValueError("exhaustive must be 0 or 1")
                cap = search["settle_cap"]
                if full_graph == "1":
                    hub, cap = None, None
                res = q_path(con, src, dst, hub_max=hub, settle_cap=cap,
                             time_budget=seconds, policy=policy)
                self._send(200, res)
            except Exception as e:
                self._send(400, {"error": str(e),
                                 "effective_policy": policy.as_dict() if policy else None})
            finally:
                con.close()
            return

        self._send(404, {"error": "not found"})


class _Server(ThreadingHTTPServer):
    # Do NOT reuse the address: on Windows SO_REUSEADDR lets a second server
    # silently bind the same port and steal requests (serving stale code).
    # Binding must fail loudly if another instance is already running.
    allow_reuse_address = False
    daemon_threads = True


def serve(db_path, xml_path, host, port, password=None, low_mem=False):
    Handler.db_path = db_path
    Handler.xml_path = xml_path
    Handler.password = password or None
    Handler.low_mem = bool(low_mem)
    try:
        httpd = _Server((host, port), Handler)
    except OSError as e:
        print(f"[serve] ERROR: could not bind {host}:{port} ({e}). "
              f"Another server is probably still running on this port.",
              flush=True)
        sys.exit(1)
    auth_note = "Basic Auth ON" if Handler.password else "auth off"
    mem_note = "low-mem" if Handler.low_mem else "normal mem"
    print(f"[serve] http://{host}:{port}/  ({auth_note}, {mem_note}; Ctrl+C to stop)",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[serve] stopped", flush=True)
        httpd.shutdown()
