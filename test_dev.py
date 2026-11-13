"""Tests de l'agent développeur côté agent chef (dev.py), avec GitHub simulé.

Lancement : python -m unittest test_dev -v
"""
import base64
import json
import os
import tempfile
import unittest
from unittest import mock

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="test-chef-")
os.environ.pop("DATABASE_URL", None)

import app  # noqa: E402
import chef  # noqa: E402
import db  # noqa: E402
import dev  # noqa: E402


class DevTest(unittest.TestCase):
    def setUp(self):
        db.init()
        for table in ("dev_requests", "messages", "kv"):
            db.run(f"DELETE FROM {table}")
        self.appels = []
        self.runs = []
        dev._depots_cache.update(at=9e12, value=dev.DEPOTS_DEFAUT)   # pas de lecture de depots.json

        def faux_github(methode, chemin, corps=None):
            self.appels.append((methode, chemin, corps))
            return {"workflow_runs": self.runs}

        for p in (mock.patch.object(dev, "_github", faux_github),
                  mock.patch.dict(os.environ, {"DEV_GITHUB_TOKEN": "ghp_test"})):
            p.start()
            self.addCleanup(p.stop)

    def test_lancer_une_demande_declenche_le_workflow(self):
        ref = app.api_post("/api/dev", {"depot": "agent-emploi", "demande": "Trie les offres par date de publication"})["ref"]
        methode, chemin, corps = self.appels[0]
        self.assertEqual((methode, chemin), ("POST", "/actions/workflows/developpeur.yml/dispatches"))
        self.assertEqual(corps, {"ref": "main", "inputs": {
            "depot": "agent-emploi", "demande": "Trie les offres par date de publication", "id": ref}})
        item = app.api_get("/api/dev", {})["items"][0]
        self.assertEqual((item["ref"], item["statut"]), (ref, "en_cours"))

    def test_liste_des_depots_lue_dans_agent_dev(self):
        dev._depots_cache.update(at=0.0, value=None)
        contenu = base64.b64encode(json.dumps({"agent-x": {"nom": "Agent X"}}).encode()).decode()
        with mock.patch.object(dev, "_github", lambda *a: {"content": contenu}):
            self.assertEqual(dev.depots(max_age=0), {"agent-x": "Agent X"})
        # Lecture impossible : la liste intégrée sert de secours
        self.assertEqual(dev.depots(max_age=0), dev.DEPOTS_DEFAUT)
        self.assertEqual(dev._repo(), "AnthonyStocko/agent-dev")

    def test_demandes_invalides(self):
        for depot, demande in (("autre-depot", "Une demande assez longue"), ("agent-emploi", "court")):
            with self.assertRaises(dev.DevError):
                dev.submit(depot, demande)
        with mock.patch.dict(os.environ, {"DEV_GITHUB_TOKEN": ""}), self.assertRaises(dev.DevError):
            dev.submit("agent-emploi", "Une demande assez longue")
        self.assertEqual(self.appels, [])

    def test_echec_du_declenchement(self):
        def refus(*a):
            raise dev.DevError("GitHub a répondu 404")
        with mock.patch.object(dev, "_github", refus), self.assertRaises(dev.DevError):
            dev.submit("agent-perso", "Une demande assez longue")
        self.assertEqual(dev.listing()["items"][0]["statut"], "erreur")

    def test_compte_rendu_met_a_jour_et_annonce(self):
        ref = dev.submit("agent-perso", "Classe les brouillons par urgence")
        app.agent_api("POST", "/agent/dev/rapport", {}, {
            "id": ref, "depot": "agent-perso", "statut": "fusionne", "titre": "Brouillons triés",
            "resume": "Les brouillons sont triés.", "pr_url": "https://github.com/AnthonyStocko/agent-perso/pull/3",
            "run_url": "javascript:alert(1)"})
        item = dev.listing()["items"][0]
        self.assertEqual((item["statut"], item["titre"]), ("fusionne", "Brouillons triés"))
        self.assertEqual(item["run_url"], "")   # seuls les liens github.com sont gardés
        message = chef.history()[-1]
        self.assertEqual(message["agent"], "dev")
        self.assertIn("Agent perso", message["content"])
        self.assertIn("pull/3", message["content"])

    def test_compte_rendu_refuse_un_statut_inconnu(self):
        ref = dev.submit("agent-perso", "Classe les brouillons par urgence")
        with self.assertRaises(dev.DevError):
            dev.report({"id": ref, "statut": "nimporte"})

    def test_compte_rendu_d_une_demande_lancee_depuis_github(self):
        dev.report({"id": "manuel", "depot": "Monguide", "statut": "sans_modification", "resume": "Rien."})
        item = dev.listing()["items"][0]
        self.assertEqual((item["depot"], item["statut"]), ("Monguide", "sans_modification"))

    def test_suivi_retrouve_le_journal_et_les_travaux_sans_rapport(self):
        ref = dev.submit("agent-emploi", "Trie les offres par date de publication")
        db.run("UPDATE dev_requests SET created_at = ? WHERE ref = ?", ("2026-01-01T00:00:00+00:00", ref))
        self.runs = [{"display_title": f"dev-{ref} · agent-emploi", "status": "in_progress",
                      "html_url": "https://github.com/AnthonyStocko/agent-dev/actions/runs/9",
                      "updated_at": "2026-01-01T00:01:00Z"}]
        item = dev.listing()["items"][0]
        self.assertEqual((item["statut"], item["run_url"][-6:]), ("en_cours", "runs/9"))
        self.runs[0].update(status="completed", conclusion="failure")
        item = dev.listing()["items"][0]
        self.assertEqual(item["statut"], "erreur")
        self.assertIn("sans envoyer de compte rendu", item["resume"])

    def test_discussion_demande_confirmation_avant_de_lancer(self):
        plan = {"action": "dev", "depot": "agent-emploi", "demande_dev": "Trie les offres par date de publication",
                "reponse": "", "a_retenir": []}
        with mock.patch.object(chef.llm, "ask_json", return_value=plan):
            r = chef.chat("Dans l'agent emploi, trie les offres par date")
        self.assertIn("à confirmer", r["answer"])
        self.assertEqual(self.appels, [])
        r = chef.chat("oui")
        self.assertIn("Demande lancée", r["answer"])
        self.assertEqual(self.appels[0][2]["inputs"]["depot"], "agent-emploi")
        # Un « non » annule ; un depot inconnu fait poser la question
        with mock.patch.object(chef.llm, "ask_json", return_value=plan):
            chef.chat("Dans l'agent emploi, trie les offres par date")
        self.assertIn("annulée", chef.chat("non")["answer"])
        with mock.patch.object(chef.llm, "ask_json", return_value={**plan, "depot": "inconnu"}):
            self.assertIn("Quel agent", chef.chat("Améliore mon agent")["answer"])
        self.assertEqual(len(self.appels), 1)


class CreerAgentTest(unittest.TestCase):
    """Création d'un agent à la demande, avec GitHub simulé."""

    def setUp(self):
        db.init()
        for table in ("dev_requests", "messages", "kv"):
            db.run(f"DELETE FROM {table}")
        self.appels, self.pannes = [], {}
        self.fichier = {"agent-emploi": {"depot": "AnthonyStocko/agent-emploi", "nom": "Agent emploi"}}
        dev._depots_cache.update(at=9e12, value=dev.DEPOTS_DEFAUT)

        def faux_github(methode, chemin, corps=None, racine=False):
            self.appels.append((methode, chemin, corps, racine))
            if (methode, chemin) in self.pannes:
                raise self.pannes[(methode, chemin)]
            if chemin.startswith("/contents/depots.json") and methode == "GET":
                return {"sha": "abc", "content": base64.b64encode(json.dumps(self.fichier).encode()).decode()}
            if methode == "PUT":
                self.fichier = json.loads(base64.b64decode(corps["content"]))
            return {}

        for p in (mock.patch.object(dev, "_github", faux_github),
                  mock.patch.dict(os.environ, {"DEV_GITHUB_TOKEN": "ghp_secret"})):
            p.start()
            self.addCleanup(p.stop)
        self.plan = {"action": "creer_agent", "cle_depot": "agent-veille", "libelle": "Agent veille",
                     "description_agent": "Surveille des sites et envoie un résumé chaque matin.",
                     "reponse": "", "a_retenir": []}

    def _demander(self):
        with mock.patch.object(chef.llm, "ask_json", return_value=self.plan):
            return chef.chat("Crée un agent qui surveille des sites")

    def test_creation_reussie(self):
        r = self._demander()
        self.assertIn("AnthonyStocko/agent-veille", r["answer"])
        self.assertIn("Agent veille", r["answer"])
        self.assertEqual(self.appels, [])   # rien avant le « oui »
        r = chef.chat("oui")
        self.assertIn("Agent créé", r["answer"])
        self.assertIn("compte rendu arrivera", r["answer"])
        creation, lecture, ecriture, _relecture, lancement = self.appels
        self.assertEqual(creation, ("POST", "/user/repos", {"name": "agent-veille", "private": True,
                                    "auto_init": False, "description": "Agent veille"}, True))
        self.assertEqual(lecture[:2], ("GET", "/contents/depots.json?ref=main"))
        self.assertEqual(ecriture[:2], ("PUT", "/contents/depots.json"))
        self.assertEqual((ecriture[2]["sha"], ecriture[2]["branch"]), ("abc", "main"))
        nouveau = json.loads(base64.b64decode(ecriture[2]["content"]))
        self.assertEqual(nouveau["agent-veille"], {"depot": "AnthonyStocko/agent-veille", "nom": "Agent veille"})
        self.assertIn("agent-emploi", nouveau)   # l'existant est conservé
        self.assertIn("agent-veille", dev._depots_cache["value"])   # cache vidé puis relu
        self.assertEqual(lancement[1], "/actions/workflows/developpeur.yml/dispatches")
        entree = lancement[2]["inputs"]
        self.assertEqual(entree["depot"], "agent-veille")
        for mot in ("vide", "/healthz", "test_*.py", "README", "Surveille des sites"):
            self.assertIn(mot, entree["demande"])

    def test_cle_invalide_ou_existante(self):
        for cle in ("veille", "Agent-Veille", "agent-", "agent-dev", "agent-emploi", "agent veille", "agent-a/../b"):
            self.plan["cle_depot"] = cle
            self.assertIn("Je ne peux pas créer", self._demander()["answer"], cle)
        self.assertEqual(self.appels, [])
        with self.assertRaises(dev.DevError):
            dev.creer_agent("agent-dev", "X", "Une description assez longue")
        self.assertEqual(self.appels, [])

    def test_role_pas_clair(self):
        self.plan["description_agent"] = ""
        self.assertIn("Que doit faire", self._demander()["answer"])

    def test_depot_deja_existant_sur_github(self):
        self.pannes[("POST", "/user/repos")] = dev.DevError(
            "GitHub a répondu 422 : name already exists on this account", 422)
        self._demander()
        r = chef.chat("oui")["answer"]
        self.assertIn("existe déjà sur GitHub", r)
        self.assertIn("aucune étape n'a réussi", r)
        self.assertEqual(len(self.appels), 1)   # ni depots.json ni développement

    def test_echec_a_l_etape_2(self):
        self.pannes[("PUT", "/contents/depots.json")] = dev.DevError(
            "GitHub a répondu 409 : conflit avec ghp_secret", 409)
        self._demander()
        r = chef.chat("oui")["answer"]
        self.assertIn("✅ étape 1", r)
        self.assertIn("❌ étape 2", r)
        self.assertIn("409", r)
        self.assertNotIn("ghp_secret", r)
        self.assertFalse(any("dispatches" in a[1] for a in self.appels))
        self.assertEqual(db.run("SELECT * FROM dev_requests"), [])

    def test_annulation_par_non(self):
        self._demander()
        self.assertIn("annulée", chef.chat("non")["answer"])
        self.assertEqual(self.appels, [])
        with mock.patch.object(chef.llm, "ask_json", return_value={"action": "repondre", "reponse": "ok"}):
            self.assertNotIn("Agent créé", chef.chat("oui")["answer"])   # plus rien en attente
        self.assertEqual(self.appels, [])


if __name__ == "__main__":
    unittest.main()
