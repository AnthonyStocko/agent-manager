"""Les agents chapeautés : comment les joindre et ce qu'on peut leur demander."""
import base64
import json
import os
import secrets
import time
import urllib.error
import urllib.request


class AgentError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


# Erreurs passagères (service qui se réveille ou redémarre, erreurs Cloudflare 52x) : on peut réessayer.
TRANSIENT_CODES = {502, 503, 504, 520, 521, 522, 523, 524}
RETRY_DELAY = 30  # secondes entre deux essais
# Agent emploi : un service endormi peut mettre plus d'une minute à se réveiller, on attend jusqu'à 3 minutes
EMPLOI_RETRIES = 6


def _call_retry(*args, retries=3, **kwargs):
    """Comme _call, mais réessaie après une erreur passagère. À réserver aux appels sans écriture."""
    for attempt in range(retries + 1):
        try:
            return _call(*args, **kwargs)
        except AgentError as e:
            if e.code not in TRANSIENT_CODES or attempt == retries:
                raise
            time.sleep(RETRY_DELAY)


def _call(url, body=None, headers=None, timeout=240, raw=None, with_headers=False):
    """Appel HTTP à un agent. body : JSON ; raw : (octets, type de contenu) pour un envoi de fichier."""
    data, ctype = (json.dumps(body).encode(), "application/json") if body is not None else (None, "application/json")
    if raw is not None:
        data, ctype = raw
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": ctype, "User-Agent": "agent-chef/1.0", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text, response_headers = resp.read().decode("utf-8", "replace"), resp.headers
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(text)
            detail = detail.get("error") or detail.get("detail") or text
        except (ValueError, AttributeError):
            detail = text
        if "<html" in str(detail).lower() or "<!doctype" in str(detail).lower():
            detail = "page d'erreur du service (il redémarre ou se réveille, réessaie dans une minute)"
        raise AgentError(f"HTTP {e.code} : {str(detail)[:300]}", e.code)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise AgentError(f"injoignable ({getattr(e, 'reason', e)}). Sur l'offre gratuite de "
                         "Render, un service endormi met environ une minute à se réveiller.")
    try:
        parsed = json.loads(text) if text.strip() else {}
    except ValueError:
        raise AgentError("réponse illisible.")
    return (parsed, response_headers) if with_headers else parsed


def _multipart(fields, file):
    """Corps multipart/form-data : champs texte et un fichier {name, mime, data} dans le champ « file »."""
    boundary = "----agentchef" + secrets.token_hex(12)
    parts = []
    for key, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    filename = file["name"].replace('"', "").replace("\r", "").replace("\n", "")
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                 f'Content-Type: {file["mime"]}\r\n\r\n'.encode() + file["data"] + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class Emploi:
    key, name = "emploi", "Agent emploi"
    description = "recherche d'offres, notation, CV et lettres adaptés, suivi des candidatures, coaching"

    def __init__(self):
        self.url = (os.getenv("EMPLOI_URL") or "").rstrip("/")
        self.token = os.getenv("HUB_TOKEN") or ""

    @property
    def configured(self):
        return bool(self.url and self.token)

    def _api(self, path, body=None, timeout=240, retry=False):
        if not self.configured:
            raise AgentError("non configuré (variables EMPLOI_URL et HUB_TOKEN).")
        try:
            if retry:
                return _call_retry(self.url + path, body, {"Authorization": "Bearer " + self.token}, timeout,
                                   retries=EMPLOI_RETRIES)
            return _call(self.url + path, body, {"Authorization": "Bearer " + self.token}, timeout)
        except AgentError as e:
            if e.code == 401:
                raise AgentError("l'agent emploi refuse le jeton HUB_TOKEN (HTTP 401). Vérifie que HUB_TOKEN a la "
                                 "même valeur chez lui et ici, et que sa branche `connexion-agent-chef` est "
                                 "fusionnée et déployée.", 401)
            raise

    def summary(self):
        # Lecture seule : on réessaie si l'agent se réveille (502 sur l'offre gratuite de Render)
        return self._api("/api/summary", timeout=90, retry=True)

    def search(self):
        """Cherche de nouvelles offres puis les note."""
        # Recherche et notation sont relançables sans risque (les offres déjà connues ne sont pas dupliquées)
        report = self._api("/api/collect", {}, retry=True)
        scored = self._api("/api/score", {}, timeout=900, retry=True)
        return report, scored

    def generate(self, offer_id):
        return self._api(f"/api/offers/{int(offer_id)}/generate", {}, timeout=400)

    def import_cv(self, name, data_b64):
        """Ajoute un CV au profil : l'agent emploi le fusionne dans le CV maître."""
        return self._api("/api/profile/import", {"files": [{"name": name, "data": data_b64}]}, timeout=400)

    def add_offer(self, text):
        return self._api("/api/offers/manual", {"text": text}, timeout=400)


class Perso:
    key, name = "perso", "Agent perso"
    description = "mails, agenda, tâches, dépenses, planning de la journée, brouillons de mail"

    def __init__(self):
        self.url = (os.getenv("PERSO_URL") or "").rstrip("/")
        self.user = os.getenv("PERSO_USERNAME") or "admin"
        self.password = os.getenv("PERSO_PASSWORD") or ""

    @property
    def configured(self):
        return bool(self.url and self.password)

    def _headers(self):
        raw = f"{self.user}:{self.password}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}

    def assistant(self, text, previous=""):
        """Zone de saisie unique d'agent-perso (/assistant) : il choisit lui-même la fonctionnalité
        (dépense, tâche, mails, agenda, planning, question…). Les écritures reviennent « à confirmer »."""
        if not self.configured:
            raise AgentError("non configuré (variables PERSO_URL et PERSO_PASSWORD).")
        # Les écritures reviennent « à confirmer » : réessayer après une erreur passagère est sans risque.
        return _call_retry(self.url + "/assistant", {
            "texte": text[:1000], "precedent": previous[:1000],
            "provider": os.getenv("PERSO_PROVIDER") or "claude-pro"}, self._headers(), timeout=300)

    def confirm(self, token, confirmed=True):
        return _call(self.url + "/assistant/confirm", {"jeton": token, "confirmer": bool(confirmed)},
                     self._headers(), timeout=300)

    def confirm_draft(self, token, file):
        """Confirme un brouillon de mail en y joignant un document."""
        return _call(self.url + "/assistant/confirm-brouillon", headers=self._headers(), timeout=300,
                     raw=_multipart({"jeton": token}, file))

    def receipt(self, file, sentence=""):
        """Aperçu d'une dépense lue sur la photo d'un ticket. Cette route d'agent-perso attend la
        session de sa page (cookie + jeton anti-CSRF) : on l'ouvre comme le fait la page."""
        if not self.configured:
            raise AgentError("non configuré (variables PERSO_URL et PERSO_PASSWORD).")
        session, headers = _call(self.url + "/releve/session", headers=self._headers(), timeout=90,
                                 with_headers=True)
        cookie = next((c.split(";")[0] for c in headers.get_all("Set-Cookie") or []
                       if c.startswith("releve_session=")), "")
        if not cookie or not session.get("csrf"):
            raise AgentError("session d'envoi de photo refusée par l'agent perso.")
        return _call(self.url + "/expense/preview-photo", timeout=300,
                     headers={**self._headers(), "Cookie": cookie, "X-CSRF-Token": session["csrf"]},
                     raw=_multipart({"sentence": sentence[:500], "person": "",
                                     "provider": os.getenv("PERSO_PROVIDER") or "claude-pro"}, file))

    def confirm_expense(self, preview):
        return _call(self.url + "/expense/confirm", {
            k: preview[k] for k in ("person", "amount", "motif", "date", "row")}, self._headers(), timeout=120)

    def summary(self):
        if not self.configured:
            raise AgentError("non configuré (variables PERSO_URL et PERSO_PASSWORD).")
        _call(self.url + "/manifest.webmanifest", timeout=90)  # simple test de présence
        return {}


class Instagram:
    key, name = "instagram", "Agent Instagram Mon guide"
    description = "veille, propositions de commentaires et publications du compte Instagram de Mon guide"

    def __init__(self):
        self.url = (os.getenv("INSTAGRAM_URL") or "").rstrip("/")
        self.token = os.getenv("INSTAGRAM_TOKEN") or ""
        # Route de l'agent Instagram qui liste les commentaires à valider (modifiable sans toucher au code)
        self.comments_path = "/" + (os.getenv("INSTAGRAM_COMMENTS_PATH") or "api/comments/pending").lstrip("/")

    @property
    def configured(self):
        return bool(self.url)

    @staticmethod
    def _text(item, *keys):
        for k in keys:
            if item.get(k):
                return str(item[k]).strip()
        return ""

    def pending_comments(self):
        """Commentaires en attente de validation (lecture seule). Retourne {"count": n, "items": [...]}."""
        if not self.configured:
            raise AgentError("non configuré (variable INSTAGRAM_URL).")
        if not self.token:
            raise AgentError("accès non configuré : ajoute la variable INSTAGRAM_TOKEN (jeton d'accès de l'agent Instagram).")
        try:
            raw = _call_retry(self.url + self.comments_path, headers={"Authorization": "Bearer " + self.token},
                              timeout=90)
        except AgentError as e:
            if e.code in (401, 403):
                raise AgentError(f"l'agent Instagram refuse le jeton INSTAGRAM_TOKEN (HTTP {e.code}).", e.code)
            if e.code == 404:
                raise AgentError(f"route {self.comments_path} introuvable chez l'agent Instagram : vérifie la "
                                 "variable INSTAGRAM_COMMENTS_PATH.", 404)
            raise
        if isinstance(raw, dict):
            raw = next((raw[k] for k in ("commentaires", "comments", "items", "pending", "data")
                        if isinstance(raw.get(k), list)), [])
        items = []
        for c in raw if isinstance(raw, list) else []:
            if not isinstance(c, dict):
                continue
            items.append({
                "id": self._text(c, "id", "uuid"),
                "cible": self._text(c, "cible", "target", "compte", "account", "auteur_post", "post_author"),
                "post": self._text(c, "post", "post_url", "url", "lien", "permalink"),
                "proposition": self._text(c, "proposition", "texte", "text", "comment", "commentaire", "content"),
                "date": self._text(c, "created_at", "date", "cree_le")[:16].replace("T", " "),
            })
        return {"count": len(items), "items": items}

    def summary(self):
        return self.pending_comments()


def all_agents():
    return [Emploi(), Perso(), Instagram()]
