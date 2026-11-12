"""Tests de l'agent développeur côté agent chef (dev.py), avec GitHub simulé.

Lancement : python -m unittest test_dev -v
"""
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
                      "html_url": "https://github.com/AnthonyStocko/agent-manager/actions/runs/9",
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


if __name__ == "__main__":
    unittest.main()
