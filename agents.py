"""Les agents chapeautés : comment les joindre et ce qu'on peut leur demander."""
import base64
import json
import os
import urllib.error
import urllib.request


class AgentError(Exception):
    pass


def _call(url, body=None, headers=None, timeout=240):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json", "User-Agent": "agent-chef/1.0", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(raw)
            detail = detail.get("error") or detail.get("detail") or raw
        except ValueError:
            detail = raw
        raise AgentError(f"HTTP {e.code} : {str(detail)[:300]}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise AgentError(f"injoignable ({getattr(e, 'reason', e)}). Sur l'offre gratuite de "
                         "Render, un service endormi met environ une minute à se réveiller.")
    try:
        return json.loads(raw) if raw.strip() else {}
    except ValueError:
        raise AgentError("réponse illisible.")


class Emploi:
    key, name = "emploi", "Agent emploi"
    description = "recherche d'offres, notation, CV et lettres adaptés, suivi des candidatures, coaching"

    def __init__(self):
        self.url = (os.getenv("EMPLOI_URL") or "").rstrip("/")
        self.token = os.getenv("HUB_TOKEN") or ""

    @property
    def configured(self):
        return bool(self.url and self.token)

    def _api(self, path, body=None, timeout=240):
        if not self.configured:
            raise AgentError("non configuré (variables EMPLOI_URL et HUB_TOKEN).")
        return _call(self.url + path, body, {"Authorization": "Bearer " + self.token}, timeout)

    def summary(self):
        return self._api("/api/summary", timeout=90)

    def search(self):
        """Cherche de nouvelles offres puis les note."""
        report = self._api("/api/collect", {})
        scored = self._api("/api/score", {}, timeout=900)
        return report, scored

    def generate(self, offer_id):
        return self._api(f"/api/offers/{int(offer_id)}/generate", {}, timeout=400)


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
        return _call(self.url + "/assistant", {
            "texte": text[:1000], "precedent": previous[:1000],
            "provider": os.getenv("PERSO_PROVIDER") or "claude-pro"}, self._headers(), timeout=300)

    def confirm(self, token, confirmed=True):
        return _call(self.url + "/assistant/confirm", {"jeton": token, "confirmer": bool(confirmed)},
                     self._headers(), timeout=300)

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

    @property
    def configured(self):
        return bool(self.url)

    def summary(self):
        raise AgentError("pas encore relié : pour l'instant, seul le lien vers son interface "
                         "d'administration est disponible.")


def all_agents():
    return [Emploi(), Perso(), Instagram()]
