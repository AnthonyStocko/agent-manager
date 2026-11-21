"""Tests du curateur : budget de jetons, déduplication, compaction de la mémoire, métriques des appels.
Le modèle est simulé : aucun appel réseau.

Lancement : python -m unittest test_curateur -v
"""
import datetime
import os
import queue
import tempfile
import unittest
from unittest import mock

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="test-curateur-")
os.environ.pop("DATABASE_URL", None)

import agents  # noqa: E402
import app  # noqa: E402
import chef  # noqa: E402
import contexte  # noqa: E402
import curateur  # noqa: E402
import db  # noqa: E402
import llm  # noqa: E402
import memory  # noqa: E402
import sauvegarde  # noqa: E402


def message(_, role, texte, fil="general"):
    return db.run("INSERT INTO messages(role, content, agent, created_at, fil) VALUES(?,?,?,?,?) RETURNING id",
                  (role, texte, "", db.now(), fil))[0]["id"]


class Base(unittest.TestCase):
    def setUp(self):
        db.init()
        for t in ("memories", "memory_log", "events", "messages", "kv", "llm_metrics"):
            db.run(f"DELETE FROM {t}")
        llm.activer_metriques(True)
        self.addCleanup(llm.activer_metriques, False)


class Budget(Base):
    def test_budget_par_agent_reglable(self):
        self.assertEqual(agents.budget_tokens("chef"), agents.BUDGETS_TOKENS["chef"])
        with mock.patch.dict(os.environ, {"BUDGET_TOKENS_CHEF": "1200"}):
            self.assertEqual(agents.budget_tokens("chef"), 1200)
            self.assertEqual(curateur.budget_caracteres("chef"), 4800)
        with mock.patch.dict(os.environ, {"BUDGET_TOKENS_CHEF": "10"}):   # jamais sous le minimum
            self.assertEqual(agents.budget_tokens("chef"), agents.BUDGET_MINIMUM)
        with mock.patch.dict(os.environ, {"BUDGET_TOKENS_CHEF": "beaucoup"}):   # valeur illisible : défaut
            self.assertEqual(agents.budget_tokens("chef"), agents.BUDGETS_TOKENS["chef"])

    def test_historique_dans_le_budget_avec_derniere_demande(self):
        msgs = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"Message numéro {i} " + "x" * 200}
                for i in range(10)]
        msgs[8]["content"] = "Dernière demande de la personne"
        texte = curateur.tronquer_historique(msgs, 600)
        self.assertLessEqual(len(texte), 600)
        self.assertIn("Dernière demande de la personne", texte)
        self.assertNotIn("Message numéro 0 ", texte)

    def test_consigne_durable_conservee_meme_ancienne(self):
        msgs = [{"role": "user", "content": "Écris-moi toujours en tutoyant, jamais de formules creuses"}]
        msgs += [{"role": "assistant" if i % 2 else "user", "content": "Bavardage " + "y" * 150} for i in range(12)]
        texte = curateur.tronquer_historique(msgs, 700)
        self.assertIn("toujours en tutoyant", texte)
        self.assertLessEqual(len(texte), 700)

    def test_derniere_demande_raccourcie_si_budget_minuscule(self):
        msgs = [{"role": "user", "content": "z" * 500}]
        texte = curateur.tronquer_historique(msgs, 100)
        self.assertLessEqual(len(texte), 100)
        self.assertIn("Elle :", texte)

    def test_resume_limite_a_une_part_du_budget(self):
        texte = curateur.tronquer_historique([{"role": "user", "content": "Bonjour"}], 400, "r" * 1500)
        self.assertLessEqual(len(texte), 400)
        self.assertIn("Bonjour", texte)

    def test_preparer_respecte_le_budget_de_l_agent(self):
        for i in range(10):
            message(i, "user" if i % 2 == 0 else "assistant", f"Échange {i} " + "m" * 600)
        past = chef.history(10)
        with mock.patch.dict(os.environ, {"BUDGET_TOKENS_CHEF": "600"}):
            ctx = curateur.preparer("chef", "Et maintenant ?", past, "general")
        self.assertLessEqual(len(ctx["historique"]), 2400)
        self.assertGreater(ctx["brut"], ctx["taille"])


class Deduplication(Base):
    def souvenirs(self, *contenus, categorie="profil", importance=3):
        for c in contenus:
            memory.add(categorie, c, "manuel", importance)
        return memory.recall()

    def test_retire_ce_que_la_conversation_dit_deja(self):
        s = self.souvenirs("Il cherche un poste de chef de projet à Lyon", "Il aime les petites équipes")
        gardes = curateur.filtrer_memoire(
            s, "chef de projet Lyon petites équipes", None,
            reference="Elle : je cherche un poste de chef de projet à Lyon")
        textes = [m["content"] for m in gardes]
        self.assertNotIn("Il cherche un poste de chef de projet à Lyon", textes)
        self.assertIn("Il aime les petites équipes", textes)

    def test_le_socle_n_est_jamais_retire_a_cause_du_prompt(self):
        s = self.souvenirs("Réponds toujours en tutoyant et en restant concret", categorie="preferences")
        gardes = curateur.filtrer_memoire(s, "bonjour", None,
                                          reference="Réponds toujours en tutoyant et en restant concret")
        self.assertEqual(len(gardes), 1)

    def test_doublons_entre_souvenirs(self):
        s = self.souvenirs("Il vit à Lyon dans le quartier de la Croix-Rousse",
                           "Il vit à Lyon dans le quartier de la Croix-Rousse.")
        self.assertEqual(len(curateur.filtrer_memoire(s, "Lyon quartier", None)), 1)

    def test_filtre_selon_l_intention(self):
        memory.add("gouts_offres", "A écarté une offre de conseil en ESN", "emploi", 3)
        memory.add("habitudes", "Planifie sa semaine le dimanche soir", "perso", 3)
        memory.add("profil", "Est développeur", "manuel", 3)
        tous = memory.recall()
        emploi = [m["content"] for m in curateur.filtrer_memoire(tous, "cherche de nouvelles offres d'emploi", None)]
        self.assertIn("A écarté une offre de conseil en ESN", emploi)
        self.assertNotIn("Planifie sa semaine le dimanche soir", emploi)
        self.assertEqual(curateur.intention("note mes offres et ma candidature"), {"emploi"})
        # un fil d'espace donne la priorité à son espace
        fil = [m["content"] for m in curateur.filtrer_memoire(tous, "salut", "perso")]
        self.assertIn("Planifie sa semaine le dimanche soir", fil)

    def test_preparer_ecarte_le_hors_sujet_et_note_l_usage(self):
        memory.add("habitudes", "Planifie sa semaine le dimanche soir", "perso", 3)
        ctx = curateur.preparer("chef", "cherche des offres d'emploi à Lyon", [], "general")
        self.assertNotIn("dimanche", ctx["memoire"])
        memory.add("gouts_offres", "Préfère les offres à Lyon", "emploi", 3)
        ctx = curateur.preparer("chef", "cherche des offres d'emploi à Lyon", [], "general")
        self.assertIn("Préfère les offres à Lyon", ctx["memoire"])
        curateur.marquer_utilises_plus_tard(ctx["ids"])
        for _ in range(50):   # tâche de fond : on attend qu'elle ait écrit
            m = db.run("SELECT * FROM memories WHERE content LIKE 'Préfère%'")[0]
            if m["access_count"]:
                break
            threading_sleep()
        self.assertEqual(m["access_count"], 1)
        self.assertTrue(m["last_accessed"])

    def test_le_chef_envoie_le_contexte_allege_et_enregistre_la_metrique(self):
        memory.add("habitudes", "Planifie sa semaine le dimanche soir", "perso", 3)
        with mock.patch.object(curateur, "distiller_plus_tard", lambda fil: None), \
                mock.patch.object(contexte, "resumer_plus_tard", lambda fil: None), \
                mock.patch.object(chef.llm, "ask_json",
                                  return_value={"action": "repondre", "reponse": "Ok", "a_retenir": []}) as appel:
            chef.chat("cherche des offres d'emploi")
        self.assertNotIn("dimanche", appel.call_args[0][0])


def threading_sleep():
    import time
    time.sleep(0.05)


class Compaction(Base):
    def remplir(self, n=30, fil="general"):
        ids = [message(i, "user" if i % 2 == 0 else "assistant", f"Échange {i}", fil) for i in range(n)]
        return ids

    def test_sous_le_seuil_aucun_appel(self):
        self.remplir(15)
        with mock.patch.object(llm, "ask_json_economique") as appel:
            self.assertEqual(curateur.distiller("general", seuil=20), {"faits": 0, "purges": 0})
        appel.assert_not_called()

    def test_distillation_en_faits_atomiques(self):
        self.remplir(30)
        memory.add("profil", "Il vit à Lyon", "manuel", 3)
        faits = [{"category": "preferences", "content": "Il veut des réponses courtes", "importance": 4},
                 {"category": "profil", "content": "Il vit à Lyon.", "importance": 3},           # déjà connu
                 {"category": "profil", "content": "Il souffre d'un cancer", "importance": 3},   # sensible
                 {"category": "inconnue", "content": "Rien", "importance": 3},
                 "n'importe quoi"]
        with mock.patch.object(llm, "ask_json_economique", return_value=faits) as appel:
            r = curateur.distiller("general", seuil=20)
        self.assertEqual(r["faits"], 1)
        textes = [m["content"] for m in memory.recall()]
        self.assertIn("Il veut des réponses courtes", textes)
        self.assertFalse(any("cancer" in t for t in textes))
        self.assertEqual(len([t for t in textes if "Lyon" in t]), 1)
        self.assertIn("Échange 0", appel.call_args[0][0])
        self.assertEqual(db.run("SELECT source FROM memories WHERE content LIKE 'Il veut%'")[0]["source"], "curateur")
        # rien de nouveau à distiller au second passage
        with mock.patch.object(llm, "ask_json_economique") as appel:
            curateur.distiller("general", seuil=20)
        appel.assert_not_called()

    def test_erreur_du_modele_ne_change_rien(self):
        self.remplir(30)
        with mock.patch.object(llm, "ask_json_economique", side_effect=llm.LLMError("panne")):
            self.assertEqual(curateur.distiller("general", seuil=20), {"faits": 0, "purges": 0})
        self.assertEqual(db.kv_get("distille_fil:general", "0"), "0")
        self.assertEqual(db.run("SELECT COUNT(*) AS n FROM messages")[0]["n"], 30)

    def test_purge_seulement_ce_qui_est_resume_et_distille_hors_fenetre(self):
        ids = self.remplir(100)
        db.kv_set("resume_fil:general", '{"texte": "Résumé", "jusqu_a": %d}' % ids[69])
        with mock.patch.object(llm, "ask_json_economique", return_value=[]):
            r = curateur.distiller("general", seuil=20)
        restants = [m["id"] for m in db.run("SELECT id FROM messages ORDER BY id")]
        # résumé jusqu'au 70e message, distillé jusqu'au 90e : on peut purger les 70 premiers, mais les
        # 60 derniers (ceux que la page affiche) sont toujours gardés : 40 purgés
        self.assertEqual(r["purges"], 40)
        self.assertEqual(restants[0], ids[40])
        self.assertEqual(len(restants), 60)

    def test_pas_de_purge_sans_resume(self):
        self.remplir(100)
        with mock.patch.object(llm, "ask_json_economique", return_value=[]):
            r = curateur.distiller("general", seuil=20)
        self.assertEqual(r["purges"], 0)
        self.assertEqual(db.run("SELECT COUNT(*) AS n FROM messages")[0]["n"], 100)

    def test_modele_economique_et_etiquette_curateur(self):
        self.remplir(30)
        client = mock.MagicMock()
        reponse = mock.MagicMock(text="[]", usage_metadata=mock.MagicMock(prompt_token_count=50, candidates_token_count=2))
        client.models.generate_content.return_value = reponse
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "cle-de-test"}), \
                mock.patch("llm.get_gemini_client", return_value=client):
            curateur.distiller("general", seuil=20)
        self.assertEqual(client.models.generate_content.call_args.kwargs["model"], llm.MODELE_ECONOMIQUE)
        llm.attendre_metriques()
        self.assertEqual(db.run("SELECT agent_name FROM llm_metrics")[0]["agent_name"], "curateur")


class Hygiene(Base):
    def vieillir(self, mid, jours):
        vieux = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=jours)).isoformat(
            timespec="seconds")
        db.run("UPDATE memories SET created_at=?, updated_at=?, confirmed_at=?, last_accessed=NULL WHERE id=?",
               (vieux, vieux, vieux, mid))

    def test_fusion_des_doublons(self):
        a = memory.add("profil", "Il vit à Lyon dans le quartier de la Croix-Rousse", "manuel", 3)
        b = memory.add("profil", "Il vit à Lyon dans le quartier de la Croix-Rousse.", "manuel", 4)
        memory.add("recits", "Il vit à Lyon dans le quartier de la Croix-Rousse", "manuel", 3)   # autre catégorie
        db.run("UPDATE memories SET access_count = 2 WHERE id = ?", (a,))
        db.run("UPDATE memories SET access_count = 3 WHERE id = ?", (b,))
        r = curateur.nettoyer_memoire()
        self.assertEqual(r["fusions"], 1)
        reste = db.run("SELECT * FROM memories WHERE category = 'profil'")
        self.assertEqual(len(reste), 1)
        self.assertEqual(reste[0]["id"], b)                 # le plus important est gardé
        self.assertEqual(reste[0]["access_count"], 5)       # les usages s'additionnent
        self.assertEqual(len(db.run("SELECT * FROM memories")), 2)
        suppression = [e for e in memory.journal() if e["op"] == "delete"][0]
        self.assertEqual(suppression["source"], "curateur")
        memory.undo(suppression["id"])                      # annulable depuis le journal
        self.assertEqual(len(db.run("SELECT * FROM memories")), 3)

    def test_archivage_des_obsoletes_seulement(self):
        vieux = memory.add("recits", "A animé un atelier en 2019", "manuel", 1)
        important = memory.add("recits", "A redressé un projet en retard", "manuel", 4)
        protege = memory.add("recits", "A présenté à un jury", "manuel", 1)
        recent = memory.add("recits", "A testé un outil la semaine dernière", "manuel", 1)
        db.run("UPDATE memories SET priority = 1 WHERE id = ?", (protege,))
        for mid in (vieux, important, protege):
            self.vieillir(mid, curateur.ARCHIVE_JOURS + 30)
        r = curateur.nettoyer_memoire()
        self.assertEqual(r["archives"], 1)
        visibles = {m["id"] for m in memory.recall()}
        self.assertEqual(visibles, {important, protege, recent})
        self.assertEqual(len(memory.recall(include_expired=True)), 4)   # rien n'est supprimé
        # un souvenir consulté récemment n'est pas archivé
        memory.marquer_utilises([important])
        self.assertEqual(curateur.nettoyer_memoire()["archives"], 0)

    def test_usage_consulte_empeche_archivage(self):
        mid = memory.add("recits", "A animé un atelier en 2019", "manuel", 1)
        self.vieillir(mid, curateur.ARCHIVE_JOURS + 30)
        memory.marquer_utilises([mid])
        self.assertEqual(curateur.nettoyer_memoire()["archives"], 0)

    def test_purge_des_vieilles_metriques(self):
        llm.enregistrer_metrique("chef", "m", 1, 1, 10, 5)
        llm.attendre_metriques()
        db.run("UPDATE llm_metrics SET timestamp = '2020-01-01T00:00:00+00:00'")
        llm.enregistrer_metrique("chef", "m", 1, 1, 10, 5)
        llm.attendre_metriques()
        self.assertEqual(curateur.nettoyer_memoire()["metriques_purgees"], 1)
        self.assertEqual(db.run("SELECT COUNT(*) AS n FROM llm_metrics")[0]["n"], 1)

    def test_sauvegarde_entretient_apres_l_export(self):
        memory.add("profil", "Il vit à Lyon dans le quartier de la Croix-Rousse", "manuel", 3)
        memory.add("profil", "Il vit à Lyon dans le quartier de la Croix-Rousse.", "manuel", 3)
        fichier = os.path.join(os.environ["DATA_DIR"], "memoire.json")
        with mock.patch("builtins.print"):
            self.assertEqual(sauvegarde.main(["exporter", fichier]), 0)
        self.assertEqual(len(db.run("SELECT * FROM memories")), 1)   # fusionnés après l'export
        with open(fichier, encoding="utf-8") as f:
            self.assertEqual(len(__import__("json").load(f)["tables"]["memories"]), 2)   # sauvegarde intacte
        memory.add("profil", "Il vit à Lyon dans le quartier de la Croix-Rousse.", "manuel", 3)
        with mock.patch("builtins.print"):
            sauvegarde.main(["exporter", fichier, "--sans-entretien"])
        self.assertEqual(len(db.run("SELECT * FROM memories")), 2)

    def test_une_erreur_d_entretien_ne_fait_pas_echouer_la_sauvegarde(self):
        fichier = os.path.join(os.environ["DATA_DIR"], "memoire.json")
        with mock.patch("builtins.print"), mock.patch.object(curateur, "nettoyer_memoire", side_effect=RuntimeError):
            self.assertEqual(sauvegarde.main(["exporter", fichier]), 0)


class Metriques(Base):
    def appel_gemini(self, texte="Réponse", usage=(120, 30)):
        client = mock.MagicMock()
        meta = mock.MagicMock(prompt_token_count=usage[0], candidates_token_count=usage[1]) if usage else None
        client.models.generate_content.return_value = mock.MagicMock(text=texte, usage_metadata=meta)
        return client

    def test_enregistre_un_appel(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "cle-de-test"}), \
                mock.patch("llm.get_gemini_client", return_value=self.appel_gemini()):
            with llm.suivi("chef", contexte_chars=400, brut_chars=1000):
                llm.ask("Bonjour", model="gemini-2.5-flash")
        llm.attendre_metriques()
        ligne = db.run("SELECT * FROM llm_metrics")[0]
        self.assertEqual((ligne["agent_name"], ligne["model"]), ("chef", "gemini-2.5-flash"))
        self.assertEqual((ligne["prompt_tokens"], ligne["completion_tokens"]), (120, 30))
        self.assertEqual((ligne["context_size_chars"], ligne["context_raw_chars"]), (400, 1000))
        self.assertGreaterEqual(ligne["latency_ms"], 0)
        self.assertTrue(ligne["timestamp"])

    def test_estimation_sans_usage_du_fournisseur(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "cle-de-test"}), \
                mock.patch("llm.get_gemini_client", return_value=self.appel_gemini("abcdefgh", usage=None)):
            llm.ask("a" * 40)
        llm.attendre_metriques()
        ligne = db.run("SELECT * FROM llm_metrics")[0]
        self.assertEqual((ligne["agent_name"], ligne["prompt_tokens"], ligne["completion_tokens"]), ("inconnu", 10, 2))
        self.assertEqual(ligne["context_size_chars"], 40)

    def test_ask_json_est_mesure_aussi(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "cle-de-test"}), \
                mock.patch("llm.get_gemini_client", return_value=self.appel_gemini('{"a": 1}')):
            self.assertEqual(llm.ask_json("x", model="gemini-2.5-flash"), {"a": 1})
        llm.attendre_metriques()
        self.assertEqual(db.run("SELECT COUNT(*) AS n FROM llm_metrics")[0]["n"], 1)

    def test_desactive_n_ecrit_rien(self):
        llm.activer_metriques(False)
        self.assertFalse(llm.enregistrer_metrique("chef", "m", 1, 1, 1, 1))
        llm.attendre_metriques()
        self.assertEqual(db.run("SELECT COUNT(*) AS n FROM llm_metrics")[0]["n"], 0)

    def test_non_bloquant_file_pleine_ou_base_en_panne(self):
        with mock.patch.object(llm, "_file", queue.Queue(maxsize=1)), \
                mock.patch.object(llm, "_travailleur", {"thread": object()}):   # pas de thread : la file se remplit
            self.assertTrue(llm.enregistrer_metrique("chef", "m", 1, 1, 1, 1))
            self.assertFalse(llm.enregistrer_metrique("chef", "m", 1, 1, 1, 1))   # pleine : perdue, sans attendre
        with mock.patch.object(llm, "_ecrire_metrique", side_effect=RuntimeError("base en panne")), \
                mock.patch("builtins.print"):
            self.assertTrue(llm.enregistrer_metrique("chef", "m", 1, 1, 1, 1))
            llm.attendre_metriques()   # l'erreur est avalée par le thread d'écriture

    def test_un_echec_du_modele_n_enregistre_rien(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "cle-de-test"}), \
                mock.patch("llm.get_gemini_client", side_effect=llm.LLMError("panne")):
            with self.assertRaises(llm.LLMError):
                llm.ask("x", model="gemini-2.5-flash")
        llm.attendre_metriques()
        self.assertEqual(db.run("SELECT COUNT(*) AS n FROM llm_metrics")[0]["n"], 0)

    def test_rapport_par_agent_et_ratio(self):
        llm.enregistrer_metrique("chef", "m", 100, 20, 400, 100, context_raw_chars=1000)
        llm.enregistrer_metrique("chef", "m", 300, 40, 600, 300, context_raw_chars=1000)
        llm.enregistrer_metrique("curateur", "m", 50, 5, 200, 50)
        llm.attendre_metriques()
        r = curateur.rapport()
        chef_ = next(a for a in r["agents"] if a["agent"] == "chef")
        self.assertEqual((chef_["appels"], chef_["tokens_entree"], chef_["tokens_sortie"]), (2, 400, 60))
        self.assertEqual(chef_["contexte_moyen_chars"], 500)
        self.assertEqual(chef_["latence_moyenne_ms"], 200)
        self.assertEqual(chef_["ratio_reduction"], 0.5)
        self.assertEqual(next(a for a in r["agents"] if a["agent"] == "curateur")["ratio_reduction"], 0.0)
        self.assertEqual(r["budgets"]["chef"], agents.BUDGETS_TOKENS["chef"])
        self.assertEqual(r["ratio_reduction"], round(1 - 1200 / 2200, 3))

    def test_route_metrics(self):
        llm.enregistrer_metrique("chef", "m", 10, 2, 100, 7, context_raw_chars=200)
        llm.attendre_metriques()
        r = app.api_get("/api/metrics", {"jours": ["3"]})
        self.assertEqual(r["jours"], 3)
        self.assertEqual(r["agents"][0]["agent"], "chef")
        self.assertEqual(set(r["agents"][0]), {"agent", "appels", "tokens_entree", "tokens_sortie",
                                               "contexte_moyen_chars", "latence_moyenne_ms", "latence_max_ms",
                                               "ratio_reduction"})   # des chiffres seulement, jamais de contenu


if __name__ == "__main__":
    unittest.main()
