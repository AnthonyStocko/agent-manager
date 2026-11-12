#!/usr/bin/env python3
"""Agent développeur : réalise une demande de modification dans le dépôt d'un autre agent.

Lancé par le workflow GitHub Actions `developpeur.yml` (déclenché par l'agent chef). Étapes :
  1. clone le dépôt visé et installe ses dépendances ;
  2. confie la demande à Claude Code, qui lit et modifie le code (avec ton abonnement) ;
  3. refuse le travail s'il touche un chemin interdit (workflows, principes…) ;
  4. lance les vérifications du dépôt ; en cas d'échec, Claude Code corrige (deux essais) ;
  5. pousse une branche `dev/<id>`, ouvre une pull request et la fusionne si tout est vert ;
  6. envoie son compte rendu à l'agent chef.

Variables : DEPOT, DEMANDE, ID, DEV_GITHUB_TOKEN, CLAUDE_CODE_OAUTH_TOKEN, AGENT_URL, HUB_TOKEN.
Réglages de `depots.json` par dépôt : installation, verifications, interdits (travail refusé),
relecture (pull request laissée ouverte), attendre_ci (attendre les workflows du dépôt).
"""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ICI = os.path.dirname(os.path.abspath(__file__))
TOURS_DE_CORRECTION = 2
CLAUDE_TIMEOUT = int(os.getenv("DEV_CLAUDE_TIMEOUT", "2400"))
CI_TIMEOUT = int(os.getenv("DEV_CI_TIMEOUT", "1800"))
CI_PAUSE = int(os.getenv("DEV_CI_PAUSE", "20"))
# Claude Code ne reçoit que ces variables : ni le jeton GitHub ni HUB_TOKEN ne lui sont transmis
ENV_CLAUDE = ["PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CLI_MODEL",
              "DEV_OUTILS", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
              "pythonLocation", "PIP_BREAK_SYSTEM_PACKAGES"]
LIBELLES = {
    "fusionne": "Modification fusionnée dans main (le déploiement suit).",
    "a_relire": "Pull request ouverte : elle touche un chemin sensible, à relire et fusionner toi-même.",
    "tests_rouges": "Vérifications en échec : pull request ouverte, rien n'est fusionné.",
    "ci_rouge": "Les tests du dépôt (CI) ont échoué : pull request ouverte, rien n'est fusionné.",
    "sans_modification": "Aucune modification du code.",
    "refuse": "Travail refusé : il touche un chemin interdit. Rien n'a été poussé.",
    "erreur": "Le travail n'a pas abouti.",
}


class Echec(Exception):
    pass


def charger_depots():
    with open(os.path.join(ICI, "depots.json"), encoding="utf-8") as f:
        return json.load(f)


def sh(command, cwd=None, env=None, timeout=1800, stdin=None):
    """Lance une commande (liste, ou chaîne passée à bash). Renvoie (code, sortie)."""
    args = ["bash", "-c", command] if isinstance(command, str) else command
    try:
        p = subprocess.run(args, cwd=cwd, env=env, input=stdin, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"Délai dépassé ({timeout} s)."
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


def chemins_touches(fichiers, prefixes):
    """Fichiers modifiés qui tombent sous l'un des chemins (dossier « x/ » ou fichier exact)."""
    return sorted(f for f in fichiers
                  if any(f.startswith(p) if p.endswith("/") else f == p for p in prefixes))


def titre_et_resume(texte, demande):
    """Sépare la ligne finale « TITRE: … » du compte rendu ; à défaut, le titre vient de la demande."""
    lignes = (texte or "").strip().splitlines()
    titre = ""
    for i in range(len(lignes) - 1, -1, -1):
        m = re.match(r"^\W*TITRE\W*:\s*(.+)$", lignes[i].strip(), re.I)
        if m:
            titre = m.group(1).strip(" *`\"«»")
            del lignes[i]
            break
    if not titre:
        titre = " ".join((demande or "").split())
    if len(titre) > 72:
        titre = titre[:71].rstrip() + "…"
    return titre or "Modification de l'agent développeur", "\n".join(lignes).strip()


def consigne(reglages, demande):
    with open(os.path.join(ICI, "consigne.md"), encoding="utf-8") as f:
        texte = f.read()
    verifs = "\n".join(f"    {v}" for v in reglages["verifications"]) or "    (aucune commande définie)"
    return (texte.replace("{nom}", reglages["nom"]).replace("{depot}", reglages["depot"])
            .replace("{verifications}", verifs).replace("{interdits}", ", ".join(reglages["interdits"]) or "aucun")
            .replace("{demande}", demande))


def claude(prompt, cwd, session=None):
    """Un tour de Claude Code dans le dépôt. Renvoie (texte, identifiant de session)."""
    exe = shutil.which("claude")
    if not exe:
        raise Echec("Claude Code est introuvable sur la machine de travail.")
    if not (os.getenv("CLAUDE_CODE_OAUTH_TOKEN") or "").strip():
        raise Echec("Le secret CLAUDE_CODE_OAUTH_TOKEN manque dans le dépôt agent-manager.")
    command = [exe, "-p", "--dangerously-skip-permissions", "--output-format", "json", "--max-turns", "150"]
    if os.getenv("CLAUDE_CLI_MODEL"):
        command += ["--model", os.environ["CLAUDE_CLI_MODEL"]]
    if session:
        command += ["--resume", session]
    env = {k: os.environ[k] for k in ENV_CLAUDE if k in os.environ}
    code, out = sh(command, cwd=cwd, env=env, timeout=CLAUDE_TIMEOUT, stdin=prompt)
    data = None
    for ligne in reversed(out.splitlines()):   # le JSON du résultat est la dernière ligne utile
        try:
            data = json.loads(ligne)
        except ValueError:
            continue
        if isinstance(data, dict):
            break
        data = None
    if data is None:
        raise Echec("Claude Code n'a pas rendu de résultat lisible : " + out[-400:])
    texte = str(data.get("result") or "").strip()
    if data.get("is_error") or code != 0:
        if "limit" in texte.lower():
            raise Echec("Limite d'usage de l'abonnement Claude atteinte : " + texte[:300])
        raise Echec("Erreur Claude Code : " + (texte[:400] or f"code {code}"))
    return texte, data.get("session_id") or session


def gh_api(method, path, body=None):
    """Appel à l'API GitHub avec la commande `gh` (jeton dans GH_TOKEN). Renvoie le JSON de la réponse."""
    command = ["gh", "api", "-X", method, path]
    if body is not None:
        command += ["--input", "-"]
    code, out = sh(command, stdin=json.dumps(body) if body is not None else None, timeout=120,
                   env={**os.environ, "GH_TOKEN": os.getenv("DEV_GITHUB_TOKEN") or ""})
    if code != 0:
        raise Echec(f"GitHub a refusé {method} {path} : {out[-400:]}")
    try:
        return json.loads(out) if out.strip() else {}
    except ValueError:
        return {}


def attendre_ci(depot, sha):
    """Attend les workflows du dépôt lancés sur ce commit. Renvoie (ok, détail)."""
    debut, vus = time.time(), False
    while time.time() - debut < CI_TIMEOUT:
        runs = gh_api("GET", f"repos/{depot}/actions/runs?head_sha={sha}&per_page=50").get("workflow_runs") or []
        if runs:
            vus = True
            if all(r.get("status") == "completed" for r in runs):
                rates = [r for r in runs if r.get("conclusion") not in ("success", "skipped", "neutral")]
                if not rates:
                    return True, f"{len(runs)} workflow(s) du dépôt au vert."
                return False, "Workflows en échec :\n" + "\n".join(
                    f"- {r.get('name')} ({r.get('conclusion')}) : {r.get('html_url')}" for r in rates)
        elif not vus and time.time() - debut > 4 * CI_PAUSE:
            return True, "Aucun workflow du dépôt ne s'est lancé sur cette branche."
        time.sleep(CI_PAUSE)
    return False, "Les workflows du dépôt n'ont pas terminé à temps."


def envoyer_rapport(rapport):
    """Compte rendu : résumé du job GitHub, puis envoi à l'agent chef (trois tentatives s'il dort)."""
    texte = (f"## Agent développeur — {rapport['depot']}\n\n**{LIBELLES.get(rapport['statut'], rapport['statut'])}**\n\n"
             + (f"Pull request : {rapport['pr_url']}\n\n" if rapport.get("pr_url") else "")
             + (rapport.get("resume") or ""))
    print(texte, flush=True)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(texte + "\n")
    url, token = (os.getenv("AGENT_URL") or "").rstrip("/"), os.getenv("HUB_TOKEN") or ""
    if not url or not token:
        print("AGENT_URL ou HUB_TOKEN absent : compte rendu non envoyé à l'agent chef.", flush=True)
        return False
    req = urllib.request.Request(url + "/agent/dev/rapport", data=json.dumps(rapport).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + token,
                                          "User-Agent": "agent-developpeur/1.0"})
    for essai in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"Agent chef injoignable (tentative {essai + 1}) : {e}", flush=True)
        if essai < 2:
            time.sleep(int(os.getenv("DEV_RAPPORT_PAUSE", "60")))
    return False


def travailler(cle, demande, ident):
    """Réalise la demande. Renvoie le rapport : statut, titre, resume, pr_url."""
    reglages = charger_depots().get(cle)
    if not reglages:
        raise Echec(f"Dépôt inconnu : {cle}.")
    depot, token = reglages["depot"], os.getenv("DEV_GITHUB_TOKEN") or ""
    distant = os.getenv("DEV_REMOTE") or f"https://github.com/{depot}.git"
    # Le jeton passe en en-tête à chaque commande git : il n'est jamais écrit dans le dépôt cloné
    auth = []
    if token and distant.startswith("https://"):
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        auth = ["-c", f"http.extraheader=AUTHORIZATION: basic {basic}"]
    dossier = os.path.join(tempfile.mkdtemp(prefix="dev-"), "depot")
    code, out = sh(["git", *auth, "clone", "--depth", "1", distant, dossier], timeout=900)
    if code != 0:
        raise Echec("Clonage impossible (jeton DEV_GITHUB_TOKEN absent, expiré ou sans accès à ce dépôt ?) : "
                    + out[-300:].replace(token or "\0", "***"))

    def git(*args, timeout=300):
        code, out = sh(["git", *args], cwd=dossier, timeout=timeout)
        if code != 0:
            raise Echec(f"git {args[0]} a échoué : {out[-300:]}")
        return out

    base, branche_defaut = git("rev-parse", "HEAD"), git("rev-parse", "--abbrev-ref", "HEAD")
    with open(os.path.join(dossier, ".git", "info", "exclude"), "a", encoding="utf-8") as f:
        f.write("\n__pycache__/\n*.pyc\nnode_modules/\n.venv/\ndata.sqlite3\n")
    os.environ["DEV_OUTILS"] = ICI
    for commande in reglages["installation"]:
        code, out = sh(commande, cwd=dossier, timeout=1500)
        if code != 0:
            raise Echec(f"Installation impossible ({commande}) : {out[-400:]}")

    def fichiers_modifies():
        """Tout ce qui diffère de l'état de départ, même si Claude Code a fait un commit de lui-même."""
        git("add", "-A")
        git("reset", "-q", "--soft", base)
        return [f for f in git("diff", "--cached", "--name-only", base).splitlines() if f]

    def verifier():
        for commande in reglages["verifications"]:
            code, out = sh(commande, cwd=dossier, timeout=1500)
            if code != 0:
                return False, f"$ {commande}\n{out[-5000:]}"
        return True, ""

    texte, session = claude(consigne(reglages, demande), dossier)
    titre, resume = titre_et_resume(texte, demande)
    ok, journal, fichiers = True, "", []
    for tour in range(TOURS_DE_CORRECTION + 1):
        fichiers = fichiers_modifies()
        if not fichiers:
            return {"statut": "sans_modification", "titre": titre, "resume": resume}
        interdits = chemins_touches(fichiers, reglages["interdits"])
        if interdits:
            return {"statut": "refuse", "titre": titre, "resume":
                    "Chemins interdits modifiés : " + ", ".join(interdits) + "\n\n" + resume}
        ok, journal = verifier()
        if ok or tour == TOURS_DE_CORRECTION:
            break
        print(f"Vérifications en échec, correction n° {tour + 1}.", flush=True)
        texte, session = claude("Les vérifications échouent après ta modification. Corrige le code (pas les "
                                "vérifications), puis redonne ton compte rendu complet et la ligne TITRE.\n\n"
                                + journal, dossier, session)
        titre, resume = titre_et_resume(texte, demande)

    branche = "dev/" + (re.sub(r"[^A-Za-z0-9._-]", "-", ident) or "demande")
    git("checkout", "-q", "-B", branche)
    fichiers_modifies()
    git("-c", "user.name=Agent développeur", "-c", "user.email=agent-developpeur@users.noreply.github.com",
        "commit", "-q", "-m", f"{titre}\n\nDemande : {demande[:1500]}")
    sha = git("rev-parse", "HEAD")
    code, out = sh(["git", *auth, "push", "-q", "--force", distant, f"HEAD:refs/heads/{branche}"],
                   cwd=dossier, timeout=600)
    if code != 0:
        raise Echec("Envoi de la branche refusé (le jeton a-t-il le droit « Contents : écriture » ?) : "
                    + out[-300:].replace(token or "\0", "***"))
    corps = (f"**Demande**\n\n{demande}\n\n**Compte rendu de l'agent développeur**\n\n{resume}\n\n"
             f"**Fichiers modifiés**\n\n" + "\n".join(f"- `{f}`" for f in fichiers))
    if not ok:
        corps += f"\n\n**Vérifications en échec**\n\n```\n{journal[-3000:]}\n```"
    pr = gh_api("POST", f"repos/{depot}/pulls", {
        "title": titre if ok else "[tests en échec] " + titre, "head": branche, "base": branche_defaut,
        "body": corps[:60000]})
    rapport = {"titre": titre, "resume": resume, "pr_url": pr.get("html_url") or ""}
    if not ok:
        return {**rapport, "statut": "tests_rouges", "resume": resume + "\n\nDernière vérification en échec :\n"
                + journal[-1500:]}
    if reglages.get("attendre_ci"):
        ci_ok, detail = attendre_ci(depot, sha)
        rapport["resume"] += "\n\n" + detail
        if not ci_ok:
            return {**rapport, "statut": "ci_rouge"}
    sensibles = chemins_touches(fichiers, reglages.get("relecture") or [])
    if sensibles:
        rapport["resume"] += "\n\nÀ relire avant fusion : " + ", ".join(sensibles)
        return {**rapport, "statut": "a_relire"}
    gh_api("PUT", f"repos/{depot}/pulls/{pr['number']}/merge", {"merge_method": "squash", "commit_title": titre})
    try:
        gh_api("DELETE", f"repos/{depot}/git/refs/heads/{branche}")
    except Echec:
        pass   # la branche reste : sans gravité
    return {**rapport, "statut": "fusionne"}


def main():
    cle, ident = (os.getenv("DEPOT") or "").strip(), (os.getenv("ID") or "manuel").strip()
    demande = (os.getenv("DEMANDE") or "").strip()
    try:
        if not demande:
            raise Echec("Demande vide.")
        rapport = travailler(cle, demande, ident)
    except Echec as e:
        rapport = {"statut": "erreur", "titre": "", "resume": str(e)}
    rapport.update(id=ident, depot=cle, resume=(rapport.get("resume") or "")[:6000])
    if os.getenv("GITHUB_RUN_ID"):
        rapport["run_url"] = (f"{os.getenv('GITHUB_SERVER_URL', 'https://github.com')}/"
                              f"{os.getenv('GITHUB_REPOSITORY')}/actions/runs/{os.environ['GITHUB_RUN_ID']}")
    envoyer_rapport(rapport)
    return 0 if rapport["statut"] in ("fusionne", "a_relire", "sans_modification") else 1


if __name__ == "__main__":
    sys.exit(main())
