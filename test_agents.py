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


if __name__ == "__main__":
    unittest.main()
