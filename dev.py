"""L'agent développeur vu de l'agent chef : lancer une demande et suivre son avancement.

L'agent développeur a son propre dépôt, AnthonyStocko/agent-dev : le travail s'y exécute dans
GitHub Actions (workflow `developpeur.yml`). Ici, l'agent chef le commande : il déclenche le
workflow, garde la liste des demandes et reçoit le compte rendu (route /agent/dev/rapport,
protégée par HUB_TOKEN).
"""
import base64
import datetime
import json
import os
import secrets
import time
import urllib.error
import urllib.request

import db

WORKFLOW = "developpeur.yml"
# Dépôts modifiables : la liste de référence est `depots.json` dans agent-dev, relue toutes les dix
# minutes ; celle-ci sert tant qu'elle n'a pas pu être lue.
DEPOTS_DEFAUT = {
    "agent-emploi": "Agent emploi", "agent-perso": "Agent perso", "agent-manager": "Agent chef",
    "mon-guide-agent-instagram": "Agent Instagram Mon guide", "Monguide": "Mon guide (application)",
}
_depots_cache = {"at": 0.0, "value": None}
STATUTS = ("en_cours", "fusionne", "a_relire", "tests_rouges", "ci_rouge", "sans_modification", "refuse", "erreur")
LIBELLES = {
    "fusionne": "modification fusionnée, le déploiement suit",
    "a_relire": "pull request à relire et fusionner toi-même (chemin sensible)",
    "tests_rouges": "vérifications en échec, rien n'est fusionné",
    "ci_rouge": "tests du dépôt en échec, rien n'est fusionné",
    "sans_modification": "aucune modification du code",
    "refuse": "travail refusé (chemin interdit), rien n'a été poussé",
    "erreur": "le travail n'a pas abouti",
}


class DevError(Exception):
    pass


def depots(max_age=600):
    """Dépôts que l'agent développeur sait modifier : {clé: nom}."""
    if _depots_cache["value"] is not None and time.time() - _depots_cache["at"] < max_age:
        return _depots_cache["value"]
    value = DEPOTS_DEFAUT
    if configured():
        try:
            content = base64.b64decode(_github("GET", "/contents/depots.json")["content"])
            value = {str(k): str(v["nom"]) for k, v in json.loads(content).items()} or DEPOTS_DEFAUT
        except (DevError, ValueError, KeyError, TypeError, AttributeError):
            pass
    _depots_cache.update(at=time.time(), value=value)
    return value


def _token():
    return (os.getenv("DEV_GITHUB_TOKEN") or "").strip()


def _repo():
    return (os.getenv("DEV_REPO") or "AnthonyStocko/agent-dev").strip()


def configured():
    return bool(_token())


def _github(method, path, body=None):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{_repo()}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + _token(), "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "agent-chef/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("message") or detail
        except (ValueError, AttributeError):
            pass
        raise DevError(f"GitHub a répondu {e.code} : {str(detail)[:200]}. Vérifie DEV_GITHUB_TOKEN "
                       "(accès au dépôt agent-dev, droits Actions et Contents en écriture).")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise DevError(f"GitHub injoignable ({getattr(e, 'reason', e)}).")
    try:
        return json.loads(text) if text.strip() else {}
    except ValueError:
        return {}


def submit(depot, demande):
    """Enregistre la demande et lance le workflow. Renvoie sa référence."""
    depot, demande = str(depot or "").strip(), str(demande or "").replace("\r", "").strip()[:4000]
    if depot not in depots():
        raise DevError("Dépôt inconnu : choisis-en un dans la liste.")
    if len(demande) < 10:
        raise DevError("Décris la modification voulue en une phrase au moins.")
    if not configured():
        raise DevError("Agent développeur non configuré : ajoute la variable DEV_GITHUB_TOKEN (voir README).")
    ref, now = secrets.token_hex(4), db.now()
    db.run("INSERT INTO dev_requests(ref, depot, demande, statut, created_at, updated_at) VALUES(?,?,?,?,?,?)",
           (ref, depot, demande, "en_cours", now, now))
    try:
        _github("POST", f"/actions/workflows/{WORKFLOW}/dispatches",
                {"ref": os.getenv("DEV_REF") or "main", "inputs": {"depot": depot, "demande": demande, "id": ref}})
    except DevError as e:
        db.run("UPDATE dev_requests SET statut = 'erreur', resume = ?, updated_at = ? WHERE ref = ?",
               (str(e), db.now(), ref))
        raise
    return ref


def report(body):
    """Compte rendu envoyé par le workflow à la fin du travail : met la demande à jour et l'annonce
    dans la discussion."""
    ref, statut = str(body.get("id") or ""), str(body.get("statut") or "")
    rows = db.run("SELECT * FROM dev_requests WHERE ref = ?", (ref,))
    if statut not in STATUTS or statut == "en_cours":
        raise DevError("Statut inconnu.")
    titre, resume = str(body.get("titre") or "")[:200], str(body.get("resume") or "")[:6000]
    pr_url, run_url = _lien(body.get("pr_url")), _lien(body.get("run_url"))
    noms = depots()
    depot = rows[0]["depot"] if rows else str(body.get("depot") or "")
    if depot not in noms:
        raise DevError("Dépôt inconnu.")
    if rows:
        db.run("UPDATE dev_requests SET statut = ?, titre = ?, resume = ?, pr_url = ?, run_url = ?, updated_at = ? "
               "WHERE ref = ?", (statut, titre, resume, pr_url, run_url, db.now(), ref))
    else:   # demande lancée à la main depuis GitHub : on la garde quand même dans la liste
        ref = ref if ref and ref != "manuel" else secrets.token_hex(4)
        if db.run("SELECT ref FROM dev_requests WHERE ref = ?", (ref,)):
            ref = secrets.token_hex(4)
        db.run("INSERT INTO dev_requests(ref, depot, demande, statut, titre, resume, pr_url, run_url, created_at, "
               "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (ref, depot, "(lancée depuis GitHub)", statut, titre, resume, pr_url, run_url, db.now(), db.now()))
    text = (f"**Agent développeur — {noms[depot]}** : {LIBELLES[statut]}."
            + (f"\n\n**{titre}**" if titre else "") + (f"\n\n{resume}" if resume else "")
            + (f"\n\nPull request : {pr_url}" if pr_url else "") + (f"\nJournal : {run_url}" if run_url else ""))
    db.run("INSERT INTO messages(role, content, agent, created_at) VALUES(?,?,?,?)",
           ("assistant", text, "dev", db.now()))
    return {"ok": True}


def _lien(url):
    url = str(url or "")
    return url[:300] if url.startswith("https://github.com/") else ""


def _age(iso):
    try:
        then = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return (datetime.datetime.now(datetime.timezone.utc) - then).total_seconds()


def _refresh(pending):
    """Demandes encore « en cours » : retrouve leur exécution GitHub (lien vers le journal) et repère
    celles qui se sont terminées sans envoyer de compte rendu."""
    runs = _github("GET", f"/actions/workflows/{WORKFLOW}/runs?per_page=40").get("workflow_runs") or []
    for row in pending:
        run = next((r for r in runs if str(r.get("display_title") or "").startswith(f"dev-{row['ref']} ")), None)
        if not run:
            if _age(row["created_at"]) > 15 * 60:
                db.run("UPDATE dev_requests SET statut = 'erreur', resume = ?, updated_at = ? WHERE ref = ?",
                       ("Le workflow ne s'est pas lancé sur GitHub : vérifie l'onglet Actions du dépôt "
                        "agent-dev.", db.now(), row["ref"]))
            continue
        url = _lien(run.get("html_url"))
        if run.get("status") == "completed" and _age(run.get("updated_at")) > 5 * 60:
            db.run("UPDATE dev_requests SET statut = 'erreur', resume = ?, run_url = ?, updated_at = ? WHERE ref = ?",
                   (f"Le travail s'est terminé ({run.get('conclusion')}) sans envoyer de compte rendu : "
                    "ouvre le journal.", url, db.now(), row["ref"]))
        elif url and url != row["run_url"]:
            db.run("UPDATE dev_requests SET run_url = ? WHERE ref = ?", (url, row["ref"]))


def listing():
    rows = db.run("SELECT * FROM dev_requests ORDER BY created_at DESC LIMIT 30")
    pending = [r for r in rows if r["statut"] == "en_cours" and _age(r["created_at"]) > 20]
    note = ""
    if pending and configured():
        try:
            _refresh(pending)
            rows = db.run("SELECT * FROM dev_requests ORDER BY created_at DESC LIMIT 30")
        except DevError as e:
            note = str(e)
    return {"configured": configured(), "depots": depots(), "items": rows, "note": note,
            "actions_url": f"https://github.com/{_repo()}/actions/workflows/{WORKFLOW}"}
