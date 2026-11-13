"""L'agent développeur vu de l'agent chef : lancer une demande et suivre son avancement.

L'agent développeur a son propre dépôt, AnthonyStocko/agent-dev : le travail s'y exécute dans
GitHub Actions (workflow `developpeur.yml`). Ici, l'agent chef le commande : il déclenche le
workflow, garde la liste des demandes et reçoit le compte rendu (route /agent/dev/rapport,
protégée par HUB_TOKEN).
"""
import base64
import datetime
import io
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.request
import zipfile

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


PROPRIETAIRE = "AnthonyStocko"
CLE_AGENT = re.compile(r"agent(-[a-z0-9]+)+")


class DevError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code   # code HTTP de GitHub, quand l'erreur en vient


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


def _github(method, path, body=None, racine=False):
    """Appelle GitHub. Par défaut `path` est relatif au dépôt agent-dev (/repos/<DEV_REPO>) ;
    avec `racine=True` c'est une adresse de l'API elle-même (ex. /user/repos)."""
    req = urllib.request.Request(
        f"https://api.github.com{'' if racine else '/repos/' + _repo()}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + _token(), "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "agent-chef/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        try:
            corps = json.loads(detail)
            detail = corps.get("message") or detail
            # Le détail (ex. « name already exists on this account ») est dans le tableau « errors »
            precisions = [str(x.get("message") or x.get("code") or "") if isinstance(x, dict) else str(x)
                          for x in (corps.get("errors") or [])]
            precisions = [p for p in precisions if p]
            if precisions:
                detail = f"{detail} ({' ; '.join(precisions)})"
        except (ValueError, AttributeError, TypeError):
            pass
        raise DevError(f"GitHub a répondu {e.code} : {str(detail)[:200]}. Vérifie DEV_GITHUB_TOKEN "
                       "(accès au dépôt agent-dev, droits Actions et Contents en écriture).", e.code)
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


def nom_module(cle):
    """Nom du module Python d'un agent hébergé dans l'agent chef : la clé, tirets remplacés par des soulignés."""
    return str(cle or "").replace("-", "_")


def cle_agent_erreur(cle, hebergement="depot"):
    """Renvoie la raison pour laquelle une clé de nouvel agent est refusée, ou None si elle convient."""
    if not CLE_AGENT.fullmatch(cle or "") or len(cle) > 60:
        return "La clé doit avoir la forme agent-xxx (minuscules, chiffres et tirets)."
    if cle == "agent-dev":
        return "agent-dev est réservé à l'agent développeur."
    if cle in depots():
        return f"Le dépôt {cle} existe déjà dans la liste : je ne touche jamais à un dépôt existant."
    if hebergement == "chef" and os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                             nom_module(cle) + ".py")):
        return (f"Le fichier {nom_module(cle)}.py existe déjà dans l'agent chef : "
                "je ne remplace jamais un module existant.")
    return None


def _ajouter_depot(cle, nom):
    """Ajoute l'entrée au fichier depots.json d'agent-dev (API contents), puis vide le cache."""
    fichier = _github("GET", "/contents/depots.json?ref=main")
    liste = json.loads(base64.b64decode(fichier["content"]))
    if cle in liste:
        raise DevError(f"{cle} figure déjà dans depots.json : rien n'a été modifié.")
    liste[cle] = {"depot": f"{PROPRIETAIRE}/{cle}", "nom": nom}
    contenu = json.dumps(liste, indent=2, ensure_ascii=False) + "\n"
    _github("PUT", "/contents/depots.json", {
        "message": f"Ajoute {cle} à la liste des dépôts", "branch": "main", "sha": fichier["sha"],
        "content": base64.b64encode(contenu.encode()).decode()})
    _depots_cache.update(at=0.0, value=None)


def creer_agent(cle, libelle, description):
    """Crée un nouvel agent en trois étapes : dépôt GitHub privé vide, entrée dans depots.json d'agent-dev,
    demande de développement sur ce dépôt. S'arrête à la première erreur en disant ce qui a réussi.
    Renvoie la référence de la demande."""
    cle, libelle = str(cle or "").strip(), " ".join(str(libelle or "").split())[:80]
    description = str(description or "").replace("\r", "").strip()[:3000]
    raison = cle_agent_erreur(cle)
    if raison:
        raise DevError(raison)
    if not libelle or len(description) < 10:
        raise DevError("Il manque le libellé ou la description de l'agent.")
    if not configured():
        raise DevError("Agent développeur non configuré : ajoute la variable DEV_GITHUB_TOKEN (voir README).")
    etapes = ["créer le dépôt GitHub privé", "l'ajouter à la liste des dépôts (depots.json d'agent-dev)",
              "lancer le développement de l'agent"]

    def echec(n, e, *, existe=False):
        faites = "".join(f"\n- ✅ étape {i + 1} : {etapes[i]}" for i in range(n))
        detail = str(e).replace(_token(), "***")
        return DevError(
            f"Création de {cle} interrompue." + (faites or "\n- aucune étape n'a réussi")
            + f"\n- ❌ étape {n + 1} ({etapes[n]}) : " + ("le dépôt existe déjà sur GitHub, je n'y touche pas. " if existe else "")
            + detail + "\n- les étapes suivantes n'ont pas été faites.")

    try:
        _github("POST", "/user/repos", {"name": cle, "private": True, "auto_init": False,
                                        "description": libelle}, racine=True)
    except DevError as e:
        raise echec(0, e, existe=e.code == 422 and "already exists" in str(e).lower())
    try:
        _ajouter_depot(cle, libelle)
    except (DevError, ValueError, KeyError, TypeError) as e:
        raise echec(1, e)
    demande = (f"Crée un nouvel agent « {libelle} ». Le dépôt {PROPRIETAIRE}/{cle} est vide : tu pars de zéro. "
               "L'agent doit être en Python, sans dépendance inutile, avec un app.py qui répond sur /healthz, "
               "des tests test_*.py et un README qui liste les variables d'environnement à définir.\n\n"
               "Ce que l'agent doit faire :\n" + description)
    try:
        return submit(cle, demande)
    except DevError as e:
        raise echec(2, e)


def creer_module(cle, libelle, description):
    """Crée un nouvel agent comme module de l'agent chef : aucun dépôt, depots.json inchangé ; une seule
    demande de développement sur agent-manager. Renvoie la référence de la demande."""
    cle, libelle = str(cle or "").strip(), " ".join(str(libelle or "").split())[:80]
    description = str(description or "").replace("\r", "").strip()[:3000]
    raison = cle_agent_erreur(cle, "chef")
    if raison:
        raise DevError(raison)
    if not libelle or len(description) < 10:
        raise DevError("Il manque le libellé ou la description de l'agent.")
    if not configured():
        raise DevError("Agent développeur non configuré : ajoute la variable DEV_GITHUB_TOKEN (voir README).")
    mod = nom_module(cle)
    demande = (f"Crée un nouvel agent « {libelle} » comme module de l'agent chef, sur le modèle de devops.py :\n"
               f"1. un fichier {mod}.py à la racine, avec la bibliothèque standard autant que possible ;\n"
               f"2. une nouvelle action dans le routeur de chef.py, avec une description claire de quand "
               f"l'utiliser, et la réponse affichée dans la discussion ;\n"
               f"3. des tests test_{mod}.py avec les services externes simulés ;\n"
               "4. une section dans le README et, s'il faut des variables d'environnement, leur déclaration "
               "dans render.yaml ;\n"
               "5. sans modifier le comportement des agents et actions existants.\n\n"
               "Ce que l'agent doit faire :\n" + description)
    return submit("agent-manager", demande)


def report(body):
    """Compte rendu envoyé par le workflow à la fin du travail : met la demande à jour et l'annonce
    dans la discussion."""
    ref, statut = str(body.get("id") or ""), str(body.get("statut") or "")
    rows = db.run("SELECT * FROM dev_requests WHERE ref = ?", (ref,))
    if statut not in STATUTS or statut == "en_cours":
        raise DevError("Statut inconnu.")
    if rows and rows[0]["statut"] != "en_cours":
        return {"ok": True}   # compte rendu déjà reçu par l'autre voie (envoi direct ou lecture sur GitHub)
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


class _SansRedirection(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None   # la redirection est suivie à la main, sans le jeton


def _rapport_github(run_id):
    """Compte rendu d'une exécution terminée, lu dans son artefact « rapport » (un zip contenant
    rapport.json). Renvoie None s'il n'y en a pas."""
    artefacts = _github("GET", f"/actions/runs/{int(run_id)}/artifacts").get("artifacts") or []
    artefact = next((a for a in artefacts if a.get("name") == "rapport" and not a.get("expired")), None)
    if not artefact:
        return None
    # GitHub répond par une redirection vers son stockage : le jeton ne doit pas y être envoyé
    req = urllib.request.Request(
        f"https://api.github.com/repos/{_repo()}/actions/artifacts/{int(artefact['id'])}/zip",
        headers={"Authorization": "Bearer " + _token(), "Accept": "application/vnd.github+json",
                 "User-Agent": "agent-chef/1.0"})
    try:
        try:
            with urllib.request.build_opener(_SansRedirection).open(req, timeout=30) as resp:
                data = resp.read(2_000_000)
        except urllib.error.HTTPError as e:
            cible = e.headers.get("Location") if e.code in (301, 302, 303, 307) else None
            if not cible or not cible.startswith("https://"):
                raise DevError(f"GitHub a répondu {e.code} en lisant le compte rendu.")
            with urllib.request.urlopen(urllib.request.Request(cible, headers={"User-Agent": "agent-chef/1.0"}),
                                        timeout=30) as resp:
                data = resp.read(2_000_000)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            body = json.loads(archive.read("rapport.json").decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, zipfile.BadZipFile):
        raise DevError("Compte rendu illisible sur GitHub.")
    return body if isinstance(body, dict) else None


def _refresh(pending):
    """Demandes encore « en cours » : retrouve leur exécution GitHub (lien vers le journal). Si elle
    est terminée, lit son compte rendu sur GitHub (l'envoi direct à l'agent chef peut ne pas arriver),
    et à défaut la signale comme terminée sans compte rendu."""
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
        if run.get("status") == "completed":
            try:
                body = _rapport_github(run["id"])
                if body:
                    report({**body, "id": row["ref"], "run_url": url})
                    continue
            except (DevError, KeyError, TypeError, ValueError):
                pass   # on réessaiera au prochain passage, puis l'erreur ci-dessous prendra le relais
        if run.get("status") == "completed" and _age(run.get("updated_at")) > 5 * 60:
            db.run("UPDATE dev_requests SET statut = 'erreur', resume = ?, run_url = ?, updated_at = ? WHERE ref = ?",
                   (f"Le travail s'est terminé ({run.get('conclusion')}) sans envoyer de compte rendu : "
                    "ouvre le journal.", url, db.now(), row["ref"]))
        elif url and url != row["run_url"]:
            db.run("UPDATE dev_requests SET run_url = ? WHERE ref = ?", (url, row["ref"]))


_sync_at = [0.0]


def sync(min_interval=0):
    """Met à jour les demandes en cours depuis GitHub. Renvoie un message d'erreur, ou une chaîne vide.
    `min_interval` : ne rien faire si le dernier passage date de moins de tant de secondes."""
    if not configured() or time.time() - _sync_at[0] < min_interval:
        return ""
    rows = db.run("SELECT * FROM dev_requests WHERE statut = 'en_cours' ORDER BY created_at DESC LIMIT 30")
    pending = [r for r in rows if _age(r["created_at"]) > 20]
    if not pending:
        return ""
    _sync_at[0] = time.time()
    try:
        _refresh(pending)
    except DevError as e:
        return str(e)
    return ""


def listing():
    note = sync()
    rows = db.run("SELECT * FROM dev_requests ORDER BY created_at DESC LIMIT 30")
    return {"configured": configured(), "depots": depots(), "items": rows, "note": note,
            "actions_url": f"https://github.com/{_repo()}/actions/workflows/{WORKFLOW}"}
