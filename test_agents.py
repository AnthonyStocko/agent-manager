import unittest
from unittest import mock

import agents


class RetryTest(unittest.TestCase):
    def test_reessaie_apres_520(self):
        calls = [agents.AgentError("HTTP 520 : x", 520), {"ok": 1}]

        def fake(*a, **k):
            r = calls.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        with mock.patch.object(agents, "_call", fake), mock.patch.object(agents.time, "sleep") as sl:
            self.assertEqual(agents._call_retry("u"), {"ok": 1})
            sl.assert_called_once()

    def test_abandonne_apres_les_essais(self):
        with mock.patch.object(agents, "_call", side_effect=agents.AgentError("HTTP 520", 520)) as c, \
                mock.patch.object(agents.time, "sleep"):
            with self.assertRaises(agents.AgentError):
                agents._call_retry("u")
            self.assertEqual(c.call_count, 3)

    def test_pas_de_nouvel_essai_pour_401(self):
        with mock.patch.object(agents, "_call", side_effect=agents.AgentError("HTTP 401", 401)) as c:
            with self.assertRaises(agents.AgentError):
                agents._call_retry("u")
            self.assertEqual(c.call_count, 1)


class EmploiJetonTest(unittest.TestCase):
    def test_401_explique_le_jeton(self):
        with mock.patch.dict(agents.os.environ, {"EMPLOI_URL": "https://e", "HUB_TOKEN": "t"}), \
                mock.patch.object(agents, "_call", side_effect=agents.AgentError("HTTP 401 : Connexion expirée", 401)):
            with self.assertRaises(agents.AgentError) as cm:
                agents.Emploi().summary()
        self.assertIn("HUB_TOKEN", str(cm.exception))
        self.assertEqual(cm.exception.code, 401)


class InstagramTest(unittest.TestCase):
    ENV = {"INSTAGRAM_URL": "https://i/", "INSTAGRAM_TOKEN": "t"}

    def test_sans_jeton(self):
        with mock.patch.dict(agents.os.environ, {"INSTAGRAM_URL": "https://i"}), \
                mock.patch.dict(agents.os.environ, {"INSTAGRAM_TOKEN": ""}):
            with self.assertRaises(agents.AgentError) as cm:
                agents.Instagram().pending_comments()
        self.assertIn("INSTAGRAM_TOKEN", str(cm.exception))

    def test_liste_normalisee(self):
        raw = {"comments": [{"id": 3, "account": "@x", "text": "Bravo", "url": "https://p", "created_at": "2026-11-12T08:30:00Z"}, "?"]}
        with mock.patch.dict(agents.os.environ, self.ENV), \
                mock.patch.object(agents, "_call_retry", return_value=raw) as c:
            r = agents.Instagram().pending_comments()
        self.assertEqual(c.call_args[0][0], "https://i/api/comments/pending")
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["items"][0]["proposition"], "Bravo")
        self.assertEqual(r["items"][0]["date"], "2026-11-12 08:30")

    def test_texte_du_chat(self):
        import chef
        self.assertIn("Aucun", chef.comments_text({"items": []}))
        self.assertIn("1 commentaire(s)", chef.comments_text({"items": [{"proposition": "Bravo", "cible": "@x"}]}))


if __name__ == "__main__":
    unittest.main()
