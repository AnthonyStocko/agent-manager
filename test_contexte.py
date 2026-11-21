"""Tests du contexte (phase 3) : souvenirs pertinents dans un budget, fils de discussion, résumé glissant,
confirmations rattachées à leur fil. Le modèle est simulé.

Lancement : python -m unittest test_contexte -v
"""
import os
import tempfile
import unittest
from unittest import mock

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="test-contexte-")
os.environ.pop("DATABASE_URL", None)

import app  # noqa: E402
import chef  # noqa: E402
import contexte  # noqa: E402
import curateur  # noqa: E402
import db  # noqa: E402
import memory  # noqa: E402

REPONDRE = {"action": "repondre", "reponse": "D'accord.", "a_retenir": []}


class Base(unittest.TestCase):
    def setUp(self):
        db.init()
        for t in ("memories", "memory_log", "events", "messages", "kv"):
            db.run(f"DELETE FROM {t}")
        for cible, nom in ((contexte, "resumer_plus_tard"), (curateur, "distiller_plus_tard")):
            p = mock.patch.object(cible, nom, lambda fil: None)
            p.start()
            self.addCleanup(p.stop)

    def chat(self, message, fil="general", plan=REPONDRE):
        with mock.patch.object(chef.llm, "ask_json", return_value=plan) as appel:
            r = chef.chat(message, fil=fil)
        return r, appel.call_args[0][0]


class Mots(unittest.TestCase):
    def test_normalisation(self):
        self.assertEqual(contexte.mots("Prépare ma candidature pour les offres à Lyon"),
                         ["prepar", "candid", "offre", "lyon"])
        self.assertEqual(contexte.mots("Il a candidaté à une offre")[:2], ["candid", "offre"])
        self.assertEqual(contexte.mots("et de la le"), [])


class Pertinence(Base):
    def test_socle_puis_pertinents_dans_le_budget(self):
        memory.add("preferences", "Veut des réponses courtes", importance=2)       # socle : préférences
        memory.add("profil", "Chef de projet depuis dix ans", importance=5)          # socle : importance 5
        memory.add("gouts_offres", "Écarte les offres à Paris", importance=3)
        memory.add("recits", "A redressé un projet informatique en retard", importance=4)
        memory.add("ligne_editoriale", "Ton chaleureux sur Instagram", importance=4)
        choisis = contexte.choisir(memory.recall(limit=500), "Trouve-moi des offres à Lyon plutôt qu'à Paris",
                                   budget=95)
        # Le budget n'admet que trois souvenirs : les deux du socle, puis le seul pertinent
        self.assertEqual({m["content"] for m in choisis},
                         {"Veut des réponses courtes", "Chef de projet depuis dix ans", "Écarte les offres à Paris"})

    def test_sans_mot_commun_les_plus_importants(self):
        memory.add("recits", "Récit A", importance=2)
        memory.add("recits", "Récit B", importance=4)
        self.assertEqual([m["content"] for m in contexte.choisir(memory.recall(), "bonjour", budget=10)], ["Récit B"])

    def test_espace_du_fil(self):
        memory.add("gouts_offres", "Préfère Lyon")
        memory.add("ligne_editoriale", "Ton chaleureux")
        self.assertNotIn("chaleureux", contexte.memoire("Lyon", "emploi"))
        self.assertIn("chaleureux", contexte.memoire("Lyon", None))

    def test_route_des_agents_avec_q(self):
        for i in range(40):
            memory.add("recits", f"Récit numéro {i} sur un sujet quelconque", importance=3)
        memory.add("recits", "Négociation difficile avec un fournisseur", importance=1)
        texte = app.agent_api("GET", "/agent/memory", {"espace": ["emploi"], "q": ["négocier avec un fournisseur"]}, {})["text"]
        self.assertTrue(texte.splitlines()[1].endswith("Négociation difficile avec un fournisseur"))


class Fils(Base):
    def test_historique_et_contexte_par_fil(self):
        memory.add("gouts_offres", "Préfère Lyon")
        memory.add("ligne_editoriale", "Ton chaleureux")
        self.chat("Bonjour côté emploi", fil="emploi")
        _, prompt = self.chat("Une question Instagram", fil="mon_guide")
        self.assertNotIn("Bonjour côté emploi", prompt)          # historique propre au fil
        self.assertIn("Ton chaleureux", prompt)
        self.assertNotIn("Préfère Lyon", prompt)                  # espace du fil
        _, prompt = self.chat("Et la suite ?", fil="emploi")
        self.assertIn("Bonjour côté emploi", prompt)
        self.assertEqual([m["content"] for m in chef.history(fil="emploi")],
                         ["Bonjour côté emploi", "D'accord.", "Et la suite ?", "D'accord."])
        with self.assertRaises(ValueError):
            chef.chat("x", fil="banque")

    def test_confirmation_valable_dans_son_fil_seulement(self):
        plan = {**REPONDRE, "action": "devops_variable", "agent_devops": "perso", "nom_variable": "JOB_ALERT_SENDERS",
                "valeur_variable": "linkedin.com"}
        with mock.patch.object(chef.devops, "preparer_variable",
                               return_value={"agent": "perso", "nom": "JOB_ALERT_SENDERS", "valeur": "linkedin.com",
                                             "resume": "résumé"}), \
                mock.patch.object(chef.devops, "definir_variable", return_value={"resume": "✅ fait"}) as definir:
            self.chat("Ajoute la variable", fil="dev", plan=plan)
            r, _ = self.chat("oui", fil="emploi")                 # autre fil : ce « oui » ne confirme rien
            self.assertEqual(definir.call_count, 0)
            r = chef.chat("oui", fil="dev")                       # bon fil : confirmé sans passer par le modèle
            self.assertEqual(r["answer"], "✅ fait")
            self.assertEqual(definir.call_count, 1)

    def test_api(self):
        self.chat("Bonjour", fil="perso")
        r = app.api_get("/api/messages", {"fil": ["perso"]})
        self.assertEqual((r["fil"], len(r["messages"])), ("perso", 2))
        self.assertEqual(set(r["fils"]), {"general", "emploi", "perso", "mon_guide", "dev"})
        self.assertIn("perso", r["derniers"])
        self.assertEqual(app.api_get("/api/messages", {})["messages"], [])
        db.kv_set("resume_fil:perso", '{"texte": "x", "jusqu_a": 1}')
        app.api_post("/api/chat/clear", {"fil": "perso"})
        self.assertEqual(chef.history(fil="perso"), [])
        self.assertEqual(contexte.resume("perso")["texte"], "")
        with self.assertRaises(ValueError):
            app.api_get("/api/messages", {"fil": ["banque"]})


class Resume(Base):
    def remplir(self, n, fil="general"):
        for i in range(n):
            chef._save("user" if i % 2 == 0 else "assistant", f"Message {i}", fil=fil)

    def test_resume_glissant(self):
        self.remplir(17)                           # 10 dans la fenêtre récente, 7 anciens : pas assez
        with mock.patch.object(contexte.llm, "ask_json", return_value={"resume": "Il cherche à Lyon."}) as appel:
            self.assertFalse(contexte.resumer("general"))
            self.remplir(1)                        # 8 anciens : mise à jour
            self.assertTrue(contexte.resumer("general"))
            prompt = appel.call_args[0][0]
        self.assertIn("Message 0", prompt)
        self.assertNotIn("Message 9 ", prompt + " ")   # les messages récents ne sont pas résumés
        etat = contexte.resume("general")
        self.assertEqual(etat["texte"], "Il cherche à Lyon.")
        texte = contexte.conversation("general")
        self.assertTrue(texte.startswith("Résumé des échanges plus anciens : Il cherche à Lyon."))
        self.assertEqual(texte.count("\n"), 11)           # résumé, ligne vide, puis les 10 derniers messages
        with mock.patch.object(contexte.llm, "ask_json", return_value={"resume": "Autre"}):
            self.assertFalse(contexte.resumer("general"))   # rien de nouveau à résumer

    def test_erreur_du_modele(self):
        self.remplir(20)
        with mock.patch.object(contexte.llm, "ask_json", side_effect=contexte.llm.LLMError("panne")):
            self.assertFalse(contexte.resumer("general"))
        self.assertEqual(contexte.resume("general")["texte"], "")

    def test_fils_independants(self):
        self.remplir(20, fil="emploi")
        with mock.patch.object(contexte.llm, "ask_json", return_value={"resume": "Emploi"}):
            self.assertFalse(contexte.resumer("perso"))
            self.assertTrue(contexte.resumer("emploi"))
        self.assertEqual(contexte.resume("perso")["texte"], "")


if __name__ == "__main__":
    unittest.main()
