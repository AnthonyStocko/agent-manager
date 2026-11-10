#!/usr/bin/env python3
"""Agent chef — mémoire commune, point d'entrée unique, tableau de bord et initiatives.

En local :   python app.py   puis ouvrir http://127.0.0.1:8770
Sur Render : image Docker ; variables APP_PASSWORD, CLAUDE_CODE_OAUTH_TOKEN, DATABASE_URL,
             HUB_TOKEN, CRON_SECRET, EMPLOI_URL, PERSO_URL, PERSO_PASSWORD (voir README).
"""
import hashlib
import hmac
import html
import json
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import agents
import chef
import db
import files
import llm
import memory

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "static")


class ApiError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


def api_get(path, query):
    if path == "/api/dashboard":
        return chef.dashboard()
    if path == "/api/messages":
        return {"messages": chef.history(), "auth": bool(os.getenv("APP_PASSWORD"))}
    if path == "/api/memories":
        return {"categories": memory.CATEGORIES, "items": memory.recall(limit=500),
                "pending_events": memory.pending_count()}
    raise ApiError("Route inconnue.", 404)


def api_post(path, body):
    if path == "/api/chat":
        return chef.chat(body.get("message"), body.get("file"))
    if path == "/api/chat/clear":
        db.run("DELETE FROM messages")
        return {"ok": True}
    if path == "/api/morning":
        return chef.start_morning(force=True)
    if path == "/api/memories":
        return {"id": memory.add(body.get("category"), body.get("content"), "manuel", body.get("importance"))}
    if path == "/api/memories/process":
        return {"applied": memory.process_pending(), "pending_events": memory.pending_count()}
    m = re.fullmatch(r"/api/memories/(\d+)/(update|delete)", path)
    if m:
        if m.group(2) == "delete":
            memory.delete(int(m.group(1)))
        elif not memory.update(int(m.group(1)), body.get("content"), body.get("category"), body.get("importance")):
            raise ApiError("Souvenir introuvable.", 404)
        return {"ok": True}
    raise ApiError("Route inconnue.", 404)


# ---------- accès des autres agents (jeton HUB_TOKEN) et du planificateur (CRON_SECRET) ----------
def _bearer_ok(headers, env_name):
    expected = os.getenv(env_name) or ""
    given = (headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    return bool(expected) and secrets.compare_digest(given.encode(), expected.encode())


def agent_api(method, path, query, body):
    """Routes réservées aux agents chapeautés : lire la mémoire, envoyer un événement."""
    if method == "GET" and path == "/agent/memory":
        cats = [c for c in (query.get("categories", [""])[0]).split(",") if c] or None
        return {"text": memory.as_text(cats, int(query.get("limit", ["40"])[0]))}
    if method == "POST" and path == "/agent/events":
        memory.add_event(body.get("agent") or "inconnu", body.get("kind") or "evenement", body.get("content"))
        return {"ok": True}
    raise ApiError("Route inconnue.", 404)


# ---------- connexion (même principe que agent-perso) ----------
SESSION_COOKIE = "app_session"
LOGIN_MAX_FAILURES = 10          # par adresse IP, sur LOGIN_WINDOW_SECONDS
LOGIN_MAX_FAILURES_TOTAL = 30    # toutes adresses confondues
LOGIN_WINDOW_SECONDS = 15 * 60
_login_failures = []
_login_lock = threading.Lock()


def _credentials():
    return os.getenv("APP_USERNAME", "admin"), os.getenv("APP_PASSWORD", "")


def _session_key():
    user, password = _credentials()
    secret = os.getenv("APP_SESSION_SECRET") or f"agent-chef|{user}|{password}"
    return hashlib.sha256(secret.encode()).digest()


def session_days():
    return int(os.getenv("APP_SESSION_DAYS", "30"))


def make_session_token():
    expires = int(time.time() + session_days() * 86400)
    sig = hmac.new(_session_key(), f"session|{expires}".encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def valid_session_token(token):
    expires, _, sig = (token or "").partition(".")
    if not expires.isdigit() or int(expires) < time.time():
        return False
    expected = hmac.new(_session_key(), f"session|{expires}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def valid_credentials(user, password):
    expected_user, expected_password = _credentials()
    return (secrets.compare_digest(user.encode(), expected_user.encode())
            & secrets.compare_digest(password.encode(), expected_password.encode()))


def _too_many_failures(ip):
    with _login_lock:
        now = time.monotonic()
        _login_failures[:] = [(t, a) for t, a in _login_failures if now - t < LOGIN_WINDOW_SECONDS]
        return (len(_login_failures) >= LOGIN_MAX_FAILURES_TOTAL
                or sum(1 for _, a in _login_failures if a == ip) >= LOGIN_MAX_FAILURES)


def login_page(error=""):
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return f"""<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Connexion — Agent chef</title>
<style>body{{font:16px system-ui,sans-serif;background:#f5f6f8;display:grid;place-items:center;min-height:100vh;margin:0}}
form{{background:#fff;padding:24px;border-radius:12px;border:1px solid #e2e5ea;width:min(340px,90vw)}}
input,button{{font:inherit;width:100%;padding:10px;margin:6px 0 12px;border-radius:8px;border:1px solid #cfd4dc;box-sizing:border-box}}
button{{background:#2457d6;color:#fff;border-color:#2457d6;cursor:pointer}}.err{{color:#b42318}}h1{{font-size:20px;margin-top:0}}</style>
</head><body><form method="post" action="/login"><h1>Agent chef</h1>{err}
<label>Identifiant<input name="username" autocomplete="username" required></label>
<label>Mot de passe<input name="password" type="password" autocomplete="current-password" required></label>
<button>Se connecter</button></form></body></html>""".encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload, ctype="application/json; charset=utf-8", headers=()):
        data = payload if isinstance(payload, bytes) else json.dumps(
            payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _cookie(self, value, max_age):
        secure = "; Secure" if os.getenv("RENDER") else ""
        return ("Set-Cookie", f"{SESSION_COOKIE}={value}; Path=/; HttpOnly; SameSite=Lax; "
                              f"Max-Age={max_age}{secure}")

    def _client_ip(self):
        parts = [p.strip() for p in (self.headers.get("X-Forwarded-For") or "").split(",") if p.strip()]
        return parts[-1] if parts else self.client_address[0]

    def _session(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE:
                return valid_session_token(value)
        return False

    def _guard(self, path):
        host = self.headers.get("Host") or ""
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlsplit(origin).netloc != host:
            self._send(403, {"error": "Origine refusée."})
            return False
        if not os.getenv("APP_PASSWORD"):
            if os.getenv("RENDER"):
                self._send(503, {"error": "APP_PASSWORD n'est pas défini dans les variables "
                                          "d'environnement Render."})
                return False
            if host.split(":")[0] not in ("127.0.0.1", "localhost"):
                self._send(403, {"error": "Accès local uniquement."})
                return False
            return True
        if path == "/login" or self._session():
            return True
        if self.command == "GET" and not path.startswith("/api/"):
            self._send(303, b"", "text/plain", [("Location", "/login")])
        else:
            self._send(401, {"error": "Connexion expirée : recharge la page pour te reconnecter."})
        return False

    def _login(self):
        length = min(int(self.headers.get("Content-Length") or 0), 4096)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        ip = self._client_ip()
        if _too_many_failures(ip):
            return self._send(429, login_page("Trop de tentatives. Réessaie dans 15 minutes."), "text/html; charset=utf-8")
        if valid_credentials(form.get("username", [""])[0], form.get("password", [""])[0]):
            return self._send(303, b"", "text/plain", [
                ("Location", "/"), self._cookie(make_session_token(), session_days() * 86400)])
        with _login_lock:
            _login_failures.append((time.monotonic(), ip))
        self._send(401, login_page("Identifiant ou mot de passe incorrect."), "text/html; charset=utf-8")

    def _api(self, fn, *args):
        try:
            self._send(200, fn(*args))
        except ApiError as e:
            self._send(e.code, {"error": str(e)})
        except (llm.LLMError, agents.AgentError, files.ImportError_, ValueError) as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            print(f"Erreur interne : {e!r}", flush=True)
            self._send(500, {"error": f"Erreur interne : {e}"})

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 15_000_000:
                raise ValueError
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
            return body
        except ValueError:
            self._send(400, {"error": "Requête invalide."})
            return None

    def _machine(self, path, query):
        """Routes appelées par des programmes, protégées par jeton. Vrai si la requête est traitée."""
        if path.startswith("/agent/"):
            if not _bearer_ok(self.headers, "HUB_TOKEN"):
                self._send(401, {"error": "Jeton HUB_TOKEN manquant ou invalide."})
                return True
            body = {}
            if self.command == "POST":
                body = self._body()
                if body is None:
                    return True
            self._api(agent_api, self.command, path, query, body)
            return True
        if path == "/cron/matin" and self.command == "POST":
            if not _bearer_ok(self.headers, "CRON_SECRET"):
                self._send(401, {"error": "CRON_SECRET manquant ou invalide."})
            else:
                self._api(chef.start_morning)
            return True
        return False

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        path, query = url.path, urllib.parse.parse_qs(url.query)
        if path == "/healthz":
            return self._send(200, {"ok": True})
        if self._machine(path, query) or not self._guard(path):
            return
        if path == "/login":
            if not os.getenv("APP_PASSWORD"):
                return self._send(303, b"", "text/plain", [("Location", "/")])
            return self._send(200, login_page(), "text/html; charset=utf-8")
        if path.startswith("/api/"):
            return self._api(api_get, path, query)
        name = "index.html" if path == "/" else path.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC, name))
        if not full.startswith(STATIC + os.sep) or not os.path.isfile(full):
            return self._send(404, {"error": "Introuvable."})
        with open(full, "rb") as f:
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(200, f.read(), ctype)

    def do_HEAD(self):
        self._send(200, b"", "text/plain")

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if self._machine(path, {}) or not self._guard(path):
            return
        if path == "/login":
            return self._login()
        if path == "/logout":
            return self._send(303, b"", "text/plain", [("Location", "/login"), self._cookie("", 0)])
        body = self._body()
        if body is not None:
            self._api(api_post, path, body)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1] if len(sys.argv) > 1 else os.getenv("PORT") or 8770)
    host = os.getenv("HOST") or ("0.0.0.0" if os.getenv("RENDER") else "127.0.0.1")
    db.init()
    print(f"Agent chef prêt : http://{host}:{port}  (base : {'Postgres' if db.PG else 'SQLite locale'})", flush=True)
    try:
        ThreadingHTTPServer((host, port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt.")
