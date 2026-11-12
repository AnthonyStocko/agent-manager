"""Tests du moteur de l'agent développeur (developpeur/travail.py), sans réseau.

Claude Code et la commande `gh` sont remplacés par de faux programmes ; le dépôt visé est un dépôt
git local. Lancement : python -m unittest test_travail -v
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "developpeur"))
import travail  # noqa: E402

FAUX_CLAUDE = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
ici = os.path.dirname(os.path.abspath(__file__))
prompt = sys.stdin.read()
with open(os.path.join(ici, "scenario.json")) as f:
    etapes = json.load(f)
with open(os.path.join(ici, "appels.jsonl"), "a") as f:
    f.write(json.dumps({"args": sys.argv[1:], "prompt": prompt, "env": sorted(os.environ)}) + "\n")
etape = etapes.pop(0) if etapes else {"result": "Rien de plus.\nTITRE: rien"}
with open(os.path.join(ici, "scenario.json"), "w") as f:
    json.dump(etapes, f)
for chemin, contenu in (etape.get("write") or {}).items():
    os.makedirs(os.path.dirname(chemin) or ".", exist_ok=True)
    with open(chemin, "w") as f:
        f.write(contenu)
if etape.get("commit"):
    subprocess.run("git add -A && git -c user.name=x -c user.email=x@x commit -q -m perso", shell=True, check=True)
print(json.dumps({"result": etape.get("result", ""), "session_id": "session-1", "is_error": bool(etape.get("error"))}))
sys.exit(1 if etape.get("error") else 0)
'''

FAUX_GH = r'''#!/usr/bin/env python3
import json, os, sys
ici = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
methode, chemin = args[2], args[3]
corps = sys.stdin.read() if "--input" in args else ""
with open(os.path.join(ici, "gh.jsonl"), "a") as f:
    f.write(json.dumps({"methode": methode, "chemin": chemin, "corps": json.loads(corps) if corps else None,
                        "jeton": os.environ.get("GH_TOKEN")}) + "\n")
if methode == "POST" and chemin.endswith("/pulls"):
    print(json.dumps({"number": 7, "html_url": "https://github.com/x/y/pull/7"}))
elif "actions/runs" in chemin:
    with open(os.path.join(ici, "ci.json")) as f:
        print(f.read())
else:
    print("{}")
'''

REGLAGES = {"depot": "x/y", "nom": "Agent test", "installation": [], "verifications": ["test ! -f casse.txt"],
            "interdits": [".github/", "PRINCIPES.md"], "relecture": ["migrations/"], "attendre_ci": False}


def run(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, shell=True, check=True, capture_output=True)


class TravailTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="test-dev-")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        for nom, code in (("claude", FAUX_CLAUDE), ("gh", FAUX_GH)):
            chemin = os.path.join(self.bin, nom)
            with open(chemin, "w") as f:
                f.write(code)
            os.chmod(chemin, os.stat(chemin).st_mode | stat.S_IEXEC)
        self.ci({"workflow_runs": []})
        # Dépôt « distant » : un dépôt nu avec un premier commit sur main
        self.distant = os.path.join(self.tmp, "distant.git")
        source = os.path.join(self.tmp, "source")
        os.makedirs(source)
        run("git init -q -b main . && echo bonjour > app.txt && git add -A && "
            "git -c user.name=x -c user.email=x@x commit -q -m init", source)
        run(f"git clone -q --bare {source} {self.distant}", self.tmp)
        env = {"PATH": self.bin + os.pathsep + os.environ["PATH"], "DEV_REMOTE": self.distant,
               "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-test", "DEV_GITHUB_TOKEN": "ghp_secret",
               "HUB_TOKEN": "hub-secret", "AGENT_URL": ""}
        for p in (mock.patch.dict(os.environ, env), mock.patch.object(travail, "CI_PAUSE", 0),
                  mock.patch.object(travail, "charger_depots", lambda: {"test": self.reglages})):
            p.start()
            self.addCleanup(p.stop)
        self.reglages = dict(REGLAGES)

    def scenario(self, *etapes):
        with open(os.path.join(self.bin, "scenario.json"), "w") as f:
            json.dump(list(etapes), f)

    def ci(self, reponse):
        with open(os.path.join(self.bin, "ci.json"), "w") as f:
            json.dump(reponse, f)

    def journal(self, nom):
        chemin = os.path.join(self.bin, nom)
        if not os.path.exists(chemin):
            return []
        with open(chemin) as f:
            return [json.loads(ligne) for ligne in f]

    def branche(self, nom="dev/abc"):
        """Fichiers de la branche poussée sur le dépôt distant (None si elle n'existe pas)."""
        p = subprocess.run(["git", "ls-tree", "-r", "--name-only", nom], cwd=self.distant,
                           capture_output=True, text=True)
        return p.stdout.split() if p.returncode == 0 else None

    def test_modification_verte_fusionnee(self):
        self.scenario({"write": {"app.txt": "bonsoir\n", "test_app.txt": "t"}, "result": "J'ai changé le salut.\nTITRE: Salut du soir"})
        r = travail.travailler("test", "Dis bonsoir au lieu de bonjour", "abc")
        self.assertEqual(r["statut"], "fusionne")
        self.assertEqual(r["titre"], "Salut du soir")
        self.assertEqual(r["pr_url"], "https://github.com/x/y/pull/7")
        self.assertEqual(self.branche(), ["app.txt", "test_app.txt"])
        appels = self.journal("gh.jsonl")
        self.assertEqual([(a["methode"], a["chemin"]) for a in appels], [
            ("POST", "repos/x/y/pulls"), ("PUT", "repos/x/y/pulls/7/merge"),
            ("DELETE", "repos/x/y/git/refs/heads/dev/abc")])
        self.assertEqual(appels[0]["corps"]["base"], "main")
        self.assertEqual(appels[1]["corps"]["merge_method"], "squash")
        self.assertEqual(appels[0]["jeton"], "ghp_secret")

    def test_claude_ne_voit_ni_jeton_github_ni_hub_token(self):
        self.scenario({"write": {"app.txt": "x"}, "result": "ok\nTITRE: t"})
        travail.travailler("test", "Une demande assez longue", "abc")
        appel = self.journal("appels.jsonl")[0]
        self.assertNotIn("DEV_GITHUB_TOKEN", appel["env"])
        self.assertNotIn("HUB_TOKEN", appel["env"])
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", appel["env"])
        self.assertIn("Une demande assez longue", appel["prompt"])
        self.assertIn("test ! -f casse.txt", appel["prompt"])

    def test_chemin_interdit_refuse_sans_rien_pousser(self):
        self.scenario({"write": {".github/workflows/ci.yml": "x", "app.txt": "y"}, "result": "fait\nTITRE: t"})
        r = travail.travailler("test", "Désactive les tests du dépôt", "abc")
        self.assertEqual(r["statut"], "refuse")
        self.assertIn(".github/workflows/ci.yml", r["resume"])
        self.assertIsNone(self.branche())
        self.assertEqual(self.journal("gh.jsonl"), [])

    def test_echec_corrige_au_deuxieme_tour(self):
        self.scenario({"write": {"casse.txt": "oups"}, "result": "v1\nTITRE: v1"},
                      {"write": {"app.txt": "corrigé"}, "result": "v2\nTITRE: Version corrigée"})
        # Le deuxième tour doit supprimer casse.txt : le faux Claude ne sait qu'écrire, on vérifie donc
        # avec une vérification qui exige le contenu corrigé
        self.reglages["verifications"] = ["grep -q corrigé app.txt"]
        r = travail.travailler("test", "Corrige le salut de l'application", "abc")
        self.assertEqual(r["statut"], "fusionne")
        self.assertEqual(r["titre"], "Version corrigée")
        appels = self.journal("appels.jsonl")
        self.assertEqual(len(appels), 2)
        self.assertIn("--resume", appels[1]["args"])
        self.assertIn("grep -q corrigé app.txt", appels[1]["prompt"])

    def test_echec_persistant_ouvre_une_pr_sans_fusion(self):
        self.scenario({"write": {"casse.txt": "oups"}, "result": "v1\nTITRE: Essai"})
        r = travail.travailler("test", "Une demande qui casse tout", "abc")
        self.assertEqual(r["statut"], "tests_rouges")
        self.assertEqual(len(self.journal("appels.jsonl")), 1 + travail.TOURS_DE_CORRECTION)
        appels = self.journal("gh.jsonl")
        self.assertEqual([a["methode"] for a in appels], ["POST"])
        self.assertTrue(appels[0]["corps"]["title"].startswith("[tests en échec] "))
        self.assertIn("casse.txt", self.branche())

    def test_aucune_modification(self):
        self.scenario({"result": "La demande est trop floue, je n'ai rien changé.\nTITRE: rien"})
        r = travail.travailler("test", "Améliore un peu tout ça", "abc")
        self.assertEqual(r["statut"], "sans_modification")
        self.assertIn("trop floue", r["resume"])
        self.assertIsNone(self.branche())

    def test_chemin_sensible_laisse_la_pr_ouverte(self):
        self.scenario({"write": {"migrations/002.sql": "alter table"}, "result": "migration\nTITRE: Migration"})
        r = travail.travailler("test", "Ajoute une colonne à la table", "abc")
        self.assertEqual(r["statut"], "a_relire")
        self.assertEqual([a["methode"] for a in self.journal("gh.jsonl")], ["POST"])

    def test_ci_du_depot_en_echec_bloque_la_fusion(self):
        self.reglages["attendre_ci"] = True
        self.ci({"workflow_runs": [{"status": "completed", "conclusion": "failure", "name": "CI",
                                    "html_url": "https://github.com/x/y/actions/runs/1"}]})
        self.scenario({"write": {"app.txt": "z"}, "result": "fait\nTITRE: t"})
        r = travail.travailler("test", "Change le salut de l'application", "abc")
        self.assertEqual(r["statut"], "ci_rouge")
        self.assertNotIn("PUT", [a["methode"] for a in self.journal("gh.jsonl")])

    def test_ci_du_depot_au_vert_fusionne(self):
        self.reglages["attendre_ci"] = True
        self.ci({"workflow_runs": [{"status": "completed", "conclusion": "success", "name": "CI"}]})
        self.scenario({"write": {"app.txt": "z"}, "result": "fait\nTITRE: t"})
        self.assertEqual(travail.travailler("test", "Change le salut de l'application", "abc")["statut"], "fusionne")

    def test_commit_fait_par_claude_quand_meme_controle(self):
        self.scenario({"write": {"PRINCIPES.md": "plus de principes"}, "commit": True, "result": "fait\nTITRE: t"})
        r = travail.travailler("test", "Assouplis les principes du dépôt", "abc")
        self.assertEqual(r["statut"], "refuse")

    def test_erreur_claude(self):
        self.scenario({"error": True, "result": "You have hit your usage limit"})
        with self.assertRaises(travail.Echec) as ctx:
            travail.travailler("test", "Une demande assez longue", "abc")
        self.assertIn("Limite d'usage", str(ctx.exception))

    def test_main_rend_un_rapport_et_un_code(self):
        self.scenario({"write": {"app.txt": "x"}, "result": "ok\nTITRE: t"})
        with mock.patch.dict(os.environ, {"DEPOT": "test", "DEMANDE": "Une demande assez longue", "ID": "abc"}), \
                mock.patch.object(travail, "envoyer_rapport") as envoi:
            self.assertEqual(travail.main(), 0)
        rapport = envoi.call_args[0][0]
        self.assertEqual((rapport["id"], rapport["depot"], rapport["statut"]), ("abc", "test", "fusionne"))
        with mock.patch.dict(os.environ, {"DEPOT": "inconnu", "DEMANDE": "Une demande", "ID": "abc"}), \
                mock.patch.object(travail, "envoyer_rapport") as envoi:
            self.assertEqual(travail.main(), 1)
        self.assertEqual(envoi.call_args[0][0]["statut"], "erreur")


class OutilsTest(unittest.TestCase):
    def test_chemins_touches(self):
        fichiers = [".github/workflows/ci.yml", "app.py", "PRINCIPES.md", "docs/PRINCIPES.md", "developpeurs.py"]
        self.assertEqual(travail.chemins_touches(fichiers, [".github/", "PRINCIPES.md", "developpeur/"]),
                         [".github/workflows/ci.yml", "PRINCIPES.md"])

    def test_titre_et_resume(self):
        self.assertEqual(travail.titre_et_resume("Compte rendu.\n\n**TITRE:** Tri des offres", "x"),
                         ("Tri des offres", "Compte rendu."))
        titre, resume = travail.titre_et_resume("Pas de titre ici", "une demande " * 20)
        self.assertLessEqual(len(titre), 72)
        self.assertEqual(resume, "Pas de titre ici")

    def test_vrais_reglages_complets(self):
        with open(os.path.join(travail.ICI, "depots.json"), encoding="utf-8") as f:
            depots = json.load(f)
        for cle, r in depots.items():
            for champ in ("depot", "nom", "installation", "verifications", "interdits", "relecture", "attendre_ci"):
                self.assertIn(champ, r, f"{cle} : champ {champ} manquant")
            self.assertIn(".github/", r["interdits"], cle)
            self.assertTrue(r["verifications"], cle)
        # La liste du workflow et celle des réglages doivent rester identiques
        with open(os.path.join(travail.ICI, "..", ".github", "workflows", "developpeur.yml"), encoding="utf-8") as f:
            workflow = f.read()
        for cle in depots:
            self.assertIn(f"          - {cle}\n", workflow)


if __name__ == "__main__":
    unittest.main()
