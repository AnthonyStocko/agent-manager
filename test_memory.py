"""Tests de la mémoire commune : filtre des données sensibles, journal des changements, annulation,
provenance et expiration. Le modèle est simulé.

Lancement : python -m unittest test_memory -v
"""
import datetime
import os
import tempfile
import unittest
from unittest import mock

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="test-memoire-")
os.environ.pop("DATABASE_URL", None)

import app  # noqa: E402
import db  # noqa: E402
import memory  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        db.init()
        for table in ("memories", "memory_log", "events"):
            db.run(f"DELETE FROM {table}")

    def ops(self):
        return [e["op"] for e in reversed(memory.journal())]


class FiltreSensible(unittest.TestCase):
    def test_refuse(self):
        cas = {
            "Son IBAN est FR76 3000 6000 0112 3456 7890 189": "données bancaires",
            "Paye avec la carte 4111 1111 1111 1111": "données bancaires",
            "Son mot de passe Gmail est azerty": "identifiants",
            "Jeton ghp_abcdefghijklmn123 pour GitHub": "identifiants",
            "Il est en arrêt maladie depuis lundi": "santé",
            "Il prend un médicament le matin": "santé",
            "Il est catholique pratiquant": "opinions politiques ou religieuses",
            "Il vote à gauche": "opinions politiques ou religieuses",
            "Son orientation sexuelle": "vie intime",
        }
        for texte, attendu in cas.items():
            with self.subTest(texte=texte):
                self.assertEqual(memory.sensitive_reason(texte), attendu)

    def test_laisse_passer(self):
        for texte in ("Il cherche un poste de chef de projet à Lyon", "Il préfère les traitements de texte simples",
                      "Il a voté pour le changement du nom du projet", "Il milite pour un code propre",
                      "Numéro d'offre 2026112012345", "Il veut des réponses courtes, sans formule de politesse"):
            with self.subTest(texte=texte):
                self.assertIsNone(memory.sensitive_reason(texte))


class Journal(Base):
    def test_ajout_modification_suppression_journalises(self):
        mid = memory.add("profil", "Il cherche un poste à Lyon", "manuel", 4)
        self.assertTrue(memory.update(mid, "Il cherche un poste à Lyon ou en télétravail", importance=5))
        self.assertTrue(memory.delete(mid))
        self.assertFalse(memory.delete(mid))
        j = memory.journal()
        self.assertEqual([e["op"] for e in j], ["delete", "update", "add"])
        self.assertEqual(j[1]["before"]["content"], "Il cherche un poste à Lyon")
        self.assertEqual(j[1]["after"]["content"], "Il cherche un poste à Lyon ou en télétravail")
        self.assertEqual(j[0]["before"]["importance"], 5)
        self.assertTrue(all(e["undoable"] for e in j))

    def test_annuler_un_ajout(self):
        mid = memory.add("profil", "Il cherche un poste à Lyon")
        memory.undo(memory.journal()[0]["id"])
        self.assertEqual(memory.recall(), [])
        self.assertEqual(self.ops(), ["add", "annulation"])
        self.assertIsNotNone(db.run("SELECT undone_at FROM memory_log WHERE op = 'add'")[0]["undone_at"])
        self.assertIsNone(memory._get(mid))

    def test_annuler_une_modification(self):
        mid = memory.add("profil", "Version 1", importance=2)
        memory.update(mid, "Version 2", importance=5)
        memory.undo(memory.journal()[0]["id"])
        m = memory._get(mid)
        self.assertEqual((m["content"], m["importance"]), ("Version 1", 2))

    def test_annuler_une_suppression_recree_le_meme_souvenir(self):
        mid = memory.add("recits", "Il a redressé un projet en retard de trois mois", importance=4)
        memory.delete(mid, "chef")
        memory.undo(memory.journal()[0]["id"])
        m = memory._get(mid)
        self.assertEqual((m["id"], m["content"], m["importance"]), (mid, "Il a redressé un projet en retard de trois mois", 4))
        self.assertEqual(memory.add("profil", "Un autre"), mid + 1)   # les identifiants continuent normalement

    def test_annulation_unique_et_dans_l_ordre(self):
        mid = memory.add("profil", "Version 1")
        memory.update(mid, "Version 2")
        memory.delete(mid)
        log_update = [e for e in memory.journal() if e["op"] == "update"][0]["id"]
        with self.assertRaises(ValueError):
            memory.undo(log_update)             # supprimé depuis : il faut d'abord annuler la suppression
        memory.undo(memory.journal()[0]["id"])  # annule la suppression
        with self.assertRaises(ValueError):
            memory.undo(memory.journal()[1]["id"])   # déjà annulé
        memory.undo(log_update)
        self.assertEqual(memory._get(mid)["content"], "Version 1")
        with self.assertRaises(ValueError):
            memory.undo(999999)

    def test_contenus_effaces_apres_30_jours(self):
        memory.add("profil", "Il cherche un poste à Lyon")
        vieux = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=31)).isoformat(timespec="seconds")
        db.run("UPDATE memory_log SET created_at = ?", (vieux,))
        e = memory.journal()[0]
        self.assertEqual((e["op"], e["before"], e["after"], bool(e["undoable"])), ("add", None, None, False))
        with self.assertRaises(ValueError):
            memory.undo(e["id"])

    def test_saisie_manuelle_sensible_refusee_sans_trace_du_contenu(self):
        with self.assertRaises(memory.SensitiveError) as c:
            memory.add("profil", "Son mot de passe est azerty")
        self.assertIn("identifiants", str(c.exception))
        mid = memory.add("profil", "Il cherche à Lyon")
        with self.assertRaises(memory.SensitiveError):
            memory.update(mid, "Il cherche à Lyon, il est en arrêt maladie")
        self.assertEqual(memory._get(mid)["content"], "Il cherche à Lyon")
        self.assertNotIn("azerty", str(db.run("SELECT * FROM memory_log")))


class Expiration(Base):
    def test_souvenir_expire_n_est_plus_utilise(self):
        hier = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        demain = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        memory.add("profil", "Disponible jusqu'à la fin du mois", valid_until=hier)
        memory.add("profil", "En recherche active", valid_until=demain)
        memory.add("profil", "Vit à Lyon", valid_until="pas une date")
        self.assertEqual({m["content"] for m in memory.recall()}, {"En recherche active", "Vit à Lyon"})
        self.assertNotIn("fin du mois", memory.as_text())
        self.assertEqual(len(memory.recall(include_expired=True)), 3)


class Traitement(Base):
    def traiter(self, ops):
        with mock.patch.object(memory.llm, "ask_json", return_value=ops) as appel, \
                mock.patch.object(memory.threading, "Thread"):
            memory.add_event("emploi", "decision_offre", "A écarté une offre à Paris")
            memory.add_event("emploi", "correction_texte", "A raccourci sa lettre")
            n = memory.process_pending()
        return n, appel

    def test_provenance_journal_et_refus(self):
        existant = memory.add("gouts_offres", "Préfère Lyon", "manuel", 3)
        n, appel = self.traiter([
            {"op": "add", "category": "style_ecriture", "content": "Écrit des lettres courtes", "importance": 3,
             "valable_jusqu_au": None},
            {"op": "update", "id": existant, "content": "Préfère Lyon, écarte Paris", "importance": 4},
            {"op": "add", "category": "profil", "content": "Il est en arrêt maladie", "importance": 5},
            {"op": "add", "category": "inconnue", "content": "Rien", "importance": 1},
        ])
        self.assertEqual(n, 2)
        self.assertIn("Aujourd'hui : " + datetime.date.today().isoformat(), appel.call_args[0][0])
        events = ",".join(str(e["id"]) for e in db.run("SELECT id FROM events ORDER BY id"))
        style = [m for m in memory.recall() if m["category"] == "style_ecriture"][0]
        self.assertEqual((style["source"], style["event_ids"]), ("emploi", events))
        maj = memory._get(existant)
        self.assertEqual(maj["event_ids"], events)
        self.assertIsNotNone(maj["confirmed_at"])
        self.assertEqual(self.ops(), ["add", "add", "update", "refus"])
        refus = memory.journal()[0]
        self.assertIn("santé", refus["reason"])
        self.assertEqual((refus["before"], refus["after"]), (None, None))
        self.assertNotIn("arrêt maladie", str(db.run("SELECT * FROM memories")) + str(db.run("SELECT * FROM memory_log")))
        self.assertEqual(memory.pending_count(), 0)

    def test_fait_temporaire(self):
        demain = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
        self.traiter([{"op": "add", "category": "profil", "content": "Disponible en décembre", "importance": 3,
                       "valable_jusqu_au": demain}])
        self.assertEqual(memory.recall()[0]["valid_until"], demain)


class Espaces(Base):
    def test_decoupage(self):
        self.assertEqual(set(memory.CATEGORIE_ESPACE), set(memory.CATEGORIES))
        self.assertEqual(set(memory.CATEGORIE_ESPACE.values()), set(memory.ESPACES))
        self.assertEqual(memory.categories_de("emploi"), ["profil", "preferences", "style_ecriture", "gouts_offres",
                                                          "recits", "points_a_travailler"])
        self.assertEqual(memory.categories_de("mon_guide"), ["profil", "preferences", "style_ecriture", "ligne_editoriale"])
        self.assertEqual(memory.categories_ecrites_par("instagram"), memory.categories_de("mon_guide"))
        self.assertEqual(memory.categories_ecrites_par("devops"), memory.categories_de("dev"))
        self.assertEqual(memory.categories_ecrites_par("chef"), list(memory.CATEGORIES))
        self.assertEqual(memory.categories_ecrites_par("inconnu"), memory.categories_de("commun"))
        with self.assertRaises(ValueError):
            memory.categories_de("banque")

    def test_lecture_par_espace(self):
        memory.add("profil", "Chef de projet")
        memory.add("gouts_offres", "Préfère Lyon")
        memory.add("ligne_editoriale", "Ton chaleureux, pas de superlatifs")
        self.assertEqual({m["content"] for m in memory.recall(espace="mon_guide")},
                         {"Chef de projet", "Ton chaleureux, pas de superlatifs"})
        texte = memory.as_text(espace="emploi")
        self.assertIn("Préfère Lyon", texte)
        self.assertNotIn("chaleureux", texte)
        self.assertEqual(len(memory.recall()), 3)   # l'agent chef lit tout

    def test_un_agent_n_ecrit_que_dans_le_commun_et_son_espace(self):
        dev = memory.add("decisions_techniques", "Un dépôt par agent", "manuel")
        with mock.patch.object(memory.threading, "Thread"):
            memory.add_event("emploi", "decision_offre", "A écarté une offre à Paris")
        ops = [
            {"op": "add", "category": "gouts_offres", "content": "Écarte Paris", "importance": 3},
            {"op": "add", "category": "profil", "content": "Cherche un poste de chef de projet", "importance": 4},
            {"op": "add", "category": "ligne_editoriale", "content": "Hors sujet", "importance": 3},
            {"op": "update", "id": dev, "content": "Modifié par l'agent emploi"},
            {"op": "delete", "id": dev},
        ]
        with mock.patch.object(memory.llm, "ask_json", return_value=ops) as appel:
            self.assertEqual(memory.process_pending(), 2)
        prompt = appel.call_args[0][0]
        self.assertIn("gouts_offres", prompt)
        self.assertNotIn("ligne_editoriale :", prompt)
        self.assertNotIn("Un dépôt par agent", prompt)   # ne voit pas les souvenirs des autres espaces
        self.assertEqual(memory._get(dev)["content"], "Un dépôt par agent")
        self.assertEqual({m["category"] for m in memory.recall()}, {"gouts_offres", "profil", "decisions_techniques"})
        refus = [e for e in memory.journal() if e["op"] == "refus"]
        self.assertEqual(len(refus), 1)
        self.assertIn("hors de son espace", refus[0]["reason"])

    def test_lots_par_agent(self):
        with mock.patch.object(memory.threading, "Thread"):
            memory.add_event("emploi", "decision_offre", "Offre 1")
            memory.add_event("chef", "conversation", "Il veut des réponses courtes")
            memory.add_event("emploi", "decision_offre", "Offre 2")
        lots = []

        def faux(prompt, **kw):
            lots.append(prompt)
            return []
        with mock.patch.object(memory.llm, "ask_json", side_effect=faux):
            memory.process_pending()
        self.assertEqual(len(lots), 2)
        self.assertIn("Offre 1", lots[0])
        self.assertIn("Offre 2", lots[0])
        self.assertNotIn("réponses courtes", lots[0])
        self.assertIn("decisions_techniques", lots[1])   # l'agent chef peut écrire partout
        self.assertEqual(memory.pending_count(), 0)

    def test_route_des_agents(self):
        memory.add("profil", "Chef de projet")
        memory.add("ligne_editoriale", "Ton chaleureux")
        texte = app.agent_api("GET", "/agent/memory", {"espace": ["emploi"]}, {})["text"]
        self.assertIn("Chef de projet", texte)
        self.assertNotIn("chaleureux", texte)
        texte = app.agent_api("GET", "/agent/memory", {"categories": ["ligne_editoriale"]}, {})["text"]
        self.assertEqual(texte.count("- "), 1)
        with self.assertRaises(ValueError):
            app.agent_api("GET", "/agent/memory", {"espace": ["banque"]}, {})


class Api(Base):
    def test_routes(self):
        mid = app.api_post("/api/memories", {"category": "profil", "content": "Vit à Lyon", "importance": 4})["id"]
        app.api_post(f"/api/memories/{mid}/update", {"content": "Vit à Lyon (Croix-Rousse)"})
        j = app.api_get("/api/memories/journal", {})
        self.assertEqual(j["days"], 30)
        self.assertEqual([e["op"] for e in j["items"]], ["update", "add"])
        app.api_post(f"/api/memories/journal/{j['items'][0]['id']}/undo", {})
        self.assertEqual(memory._get(mid)["content"], "Vit à Lyon")
        with self.assertRaises(ValueError):
            app.api_post("/api/memories", {"category": "profil", "content": "Son IBAN est FR76 3000 6000 0112 3456 7890 189"})
        with self.assertRaises(app.ApiError):
            app.api_post("/api/memories/999999/delete", {})
        liste = app.api_get("/api/memories", {})
        self.assertIn("today", liste)
        self.assertEqual(set(liste["espaces"]), {"commun", "emploi", "perso", "mon_guide", "dev"})
        self.assertEqual(len(liste["items"]), 1)


if __name__ == "__main__":
    unittest.main()
