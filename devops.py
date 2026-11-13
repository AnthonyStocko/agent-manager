"""Devops : vérifie qu'un correctif est bien déployé sur Render.

Pour chaque agent hébergé sur Render, compare le commit en ligne (API Render, lecture seule) au
dernier commit de `main` sur GitHub et rend un verdict. Rien n'est jamais modifié, ni sur Render ni
sur GitHub. Repris du dépôt agent-devops-perso, intégré ici pour ne pas ajouter de service Render.

Variables : RENDER_API_KEY, RENDER_SERVICE_AGENT_EMPLOI, RENDER_SERVICE_AGENT_PERSO (identifiants
srv-...). Le service de l'agent chef est connu sans réglage (RENDER_SERVICE_ID, fourni par Render).
GitHub est lu avec GITHUB_TOKEN ou, à défaut, DEV_GITHUB_TOKEN (dépôts privés).
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

RENDER_API = "https://api.render.com/v1"
GITHUB_API = "https://api.github.com"

# Agents hébergés sur Render. L'agent développeur (GitHub Actions) et l'agent Instagram (Supabase)
# n'y sont pas : ils ne sont pas suivis ici.
AGENTS = {
    "manager": {"nom": "Agent chef", "depot": "AnthonyStocko/agent-manager",
                "service_env": ("RENDER_SERVICE_AGENT_MANAGER", "RENDER_SERVICE_ID")},
    "emploi": {"nom": "Agent emploi", "depot": "AnthonyStocko/agent-emploi",
               "service_env": ("RENDER_SERVICE_AGENT_EMPLOI",)},
    "perso": {"nom": "Agent perso", "depot": "AnthonyStocko/agent-perso",
              "service_env": ("RENDER_SERVICE_AGENT_PERSO",)},
}
ECHECS = {"build_failed", "update_failed", "pre_deploy_failed"}
EN_COURS = {"created", "queued", "build_in_progress", "update_in_progress", "pre_deploy_in_progress"}
LIBELLES = {
    "live": "Live", "deactivated": "Remplacé", "canceled": "Annulé", "created": "Créé", "queued": "En file",
    "build_in_progress": "Build en cours", "update_in_progress": "Mise en ligne en cours",
    "pre_deploy_in_progress": "Pré-déploiement en cours", "build_failed": "Échec pendant le build",
    "update_failed": "Échec pendant la mise en ligne", "pre_deploy_failed": "Échec du pré-déploiement",
}


class DevopsError(Exception):
    """Erreur d'un service externe, avec un message sans secret."""


def configured():
    return bool((os.getenv("RENDER_API_KEY") or "").strip())


def service_id(key):
    for name in AGENTS[key]["service_env"]:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def _get(url, token, timeout=20):
    headers = {"Accept": "application/json", "User-Agent": "agent-chef/1.0"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as e:
        raise DevopsError(f"HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise DevopsError("service injoignable ou réponse illisible") from None


def render_deploys(service, limit=5):
    """Derniers déploiements d'un service Render, du plus récent au plus ancien."""
    url = f"{RENDER_API}/services/{urllib.parse.quote(service, safe='')}/deploys?limit={int(limit)}"
    try:
        raw = _get(url, (os.getenv("RENDER_API_KEY") or "").strip()) or []
    except DevopsError as e:
        raise DevopsError(f"Render : {e}") from None
    # Render renvoie soit [{"deploy": {...}, "cursor": ...}], soit directement les déploiements
    return [d.get("deploy", d) for d in raw if isinstance(d, dict)]


def github_head(repo, branch="main"):
    """SHA et message du dernier commit de la branche d'un dépôt GitHub."""
    token = (os.getenv("GITHUB_TOKEN") or os.getenv("DEV_GITHUB_TOKEN") or "").strip()
    try:
        c = _get(f"{GITHUB_API}/repos/{repo}/commits/{urllib.parse.quote(branch, safe='')}", token) or {}
    except DevopsError as e:
        raise DevopsError(f"GitHub : {e}") from None
    return {"sha": c.get("sha") or "", "message": ((c.get("commit") or {}).get("message") or "").split("\n")[0]}


def _brief(d):
    commit, status = d.get("commit") or {}, d.get("status") or "inconnu"
    return {"commit": commit.get("id") or "", "message": (commit.get("message") or "").split("\n")[0],
            "statut": status, "statut_libelle": LIBELLES.get(status, status),
            "date": d.get("finishedAt") or d.get("createdAt") or ""}


def _same(a, b):
    return bool(a) and bool(b) and (a.startswith(b) or b.startswith(a))


def verdict(last, live, reference):
    """Renvoie (code, phrase) : le commit attendu (`reference`, SHA ou début de SHA) est-il en ligne ?"""
    if last is None:
        return "inconnu", "aucun déploiement trouvé sur Render."
    status = last["statut"]
    if status in ECHECS:
        online = f" Toujours en ligne : {live['commit'][:7]}." if live else ""
        return "echec", f"le dernier déploiement a échoué ({last['statut_libelle']}, commit {last['commit'][:7]}).{online}"
    if status in EN_COURS:
        return "en_cours", f"déploiement en cours ({last['statut_libelle']}), commit {last['commit'][:7]}."
    if status != "live" or live is None:
        return "inconnu", f"statut du dernier déploiement : {last['statut_libelle']}."
    if not reference:
        return "inconnu", f"commit {live['commit'][:7]} en ligne, mais commit de référence introuvable."
    if _same(live["commit"], reference):
        return "a_jour", f"le commit {reference[:7]} est en ligne."
    return "commit_different", f"commit en ligne {live['commit'][:7]}, commit attendu {reference[:7]}."


def check(key, expected=""):
    agent = AGENTS[key]
    res = {"agent": key, "nom": agent["nom"], "depot": agent["depot"]}
    service = service_id(key)
    if not service:
        res.update(verdict="non_configure", resume=f"variable {agent['service_env'][0]} non définie.")
        return res
    try:
        deploys = render_deploys(service)
    except DevopsError as e:
        res.update(verdict="erreur", resume=f"{e}.")
        return res
    last = _brief(deploys[0]) if deploys else None
    live = next((_brief(d) for d in deploys if d.get("status") == "live"), None)
    res.update(dernier_deploiement=last, en_ligne=live)
    reference, github_error = expected, ""
    if not reference:
        try:
            head = github_head(agent["depot"])
            reference, res["dernier_commit_github"] = head["sha"], head
        except DevopsError as e:
            github_error = str(e)
    code, sentence = verdict(last, live, reference)
    if github_error and code != "echec":
        sentence += f" ({github_error})"
    res.update(verdict=code, resume=sentence)
    return res


def deployments(agent="", commit=""):
    """Vérifie un agent (clé de AGENTS) ou tous. `commit` : SHA attendu, sinon le dernier commit GitHub."""
    agent, commit = str(agent or "").strip(), str(commit or "").strip()
    if not configured():
        raise DevopsError("Vérification des déploiements non configurée : ajoute la variable RENDER_API_KEY "
                          "(Render > Account Settings > API Keys).")
    if agent and agent not in AGENTS:
        raise DevopsError("Agent inconnu. Agents suivis : " + ", ".join(a["nom"] for a in AGENTS.values()) + ".")
    items = [check(k, commit) for k in ([agent] if agent else AGENTS)]
    return {"agents": items, "tout_est_a_jour": all(a["verdict"] == "a_jour" for a in items)}


ICONS = {"a_jour": "✅", "commit_different": "⚠️", "echec": "❌", "en_cours": "⏳"}


def text(result):
    """Résultat lisible dans la discussion."""
    lines = [f"- {ICONS.get(a['verdict'], '❔')} **{a['nom']}** : {a['resume']}" for a in result["agents"]]
    head = ("**Déploiements Render : tout est à jour.**" if result["tout_est_a_jour"]
            else "**Déploiements Render** :")
    return head + "\n" + "\n".join(lines)
