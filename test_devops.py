"""Tests du branchement de l'agent devops dans l'agent chef : route de l'API et discussion.

La vérification elle-même est testée dans le dépôt agent-devops-perso (paquet agent_devops, installé par
requirements.txt). Render et GitHub sont simulés.

Lancement : python -m unittest test_devops -v
"""
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="test-chef-"))
os.environ.pop("DATABASE_URL", None)

import agent_devops  # noqa: E402
from agent_devops import clients  # noqa: E402

import app  # noqa: E402
import chef  # noqa: E402
import db  # noqa: E402

SHA_A, SHA_B = "a" * 40, "b" * 40


def deploy(sha, status):
    return {"deploy": {"commit": {"id": sha, "message": "Correctif\n\ndétail"}, "status": status,
                       "finishedAt": "2026-11-13T10:00:00Z"}}


class DevopsTest(unittest.TestCase):
    def setUp(self):
        self.render = {"srv-chef": [deploy(SHA_A, "live")], "srv-emploi": [deploy(SHA_A, "live")],
                       "srv-perso": [deploy(SHA_A, "live")]}
        self.github = {"AnthonyStocko/agent-manager": SHA_A, "AnthonyStocko/agent-emploi": SHA_A,
                       "AnthonyStocko/agent-perso": SHA_A}
        self.appels = []

        def faux_get(url, token, timeout=20):
            self.appels.append((url, token))
            if url.startswith(clients.RENDER_API):
                service = url.split("/services/")[1].split("/")[0]
                if service not in self.render:
                    raise clients.ErreurApi("HTTP 404")
                return self.render[service]
            depot = url.split("/repos/")[1].split("/commits/")[0]
            return {"sha": self.github[depot], "commit": {"message": "Correctif"}}

        env = {"RENDER_API_KEY": "rnd_secret", "RENDER_SERVICE_ID": "srv-chef", "RENDER_SERVICE_AGENT_MANAGER": "",
               "RENDER_SERVICE_AGENT_EMPLOI": "srv-emploi", "RENDER_SERVICE_AGENT_PERSO": "srv-perso",
               "GITHUB_TOKEN": "", "DEV_GITHUB_TOKEN": "ghp_dev"}
        for p in (mock.patch.object(clients, "_get", faux_get), mock.patch.dict(os.environ, env)):
            p.start()
            self.addCleanup(p.stop)

    def test_agents_suivis_et_jetons(self):
        # l'agent chef est reconnu par RENDER_SERVICE_ID ; GitHub est lu avec le jeton de l'agent développeur
        r = app.api_get("/api/devops/deploiements", {})
        self.assertTrue(r["tout_est_a_jour"])
        self.assertEqual([a["agent"] for a in r["agents"]], ["manager", "emploi", "perso"])
        self.assertEqual({t for u, t in self.appels if u.startswith(clients.RENDER_API)}, {"rnd_secret"})
        self.assertEqual({t for u, t in self.appels if u.startswith(clients.GITHUB_API)}, {"ghp_dev"})

    def test_route(self):
        r = app.api_get("/api/devops/deploiements", {"agent": ["emploi"]})
        self.assertEqual([a["agent"] for a in r["agents"]], ["emploi"])
        r = app.api_get("/api/devops/deploiements", {"agent": ["perso"], "commit": ["bbbbbbb"]})
        self.assertEqual(r["agents"][0]["verdict"], "commit_different")
        with self.assertRaises(agent_devops.ErreurDevops):
            app.api_get("/api/devops/deploiements", {"agent": ["instagram"]})

    def test_discussion(self):
        db.init()
        plan = {"action": "devops_deploiements", "agent_devops": "", "reponse": "", "a_retenir": []}
        with mock.patch.object(chef.llm, "ask_json", return_value=plan):
            reponse = chef.chat("Est-ce que mes agents sont bien déployés ?")
        self.assertIn("tout est à jour", reponse["answer"])
        self.assertEqual(chef.history()[-1]["agent"], "devops")
        self.github["AnthonyStocko/agent-emploi"] = SHA_B
        with mock.patch.object(chef.llm, "ask_json", return_value={**plan, "agent_devops": "emploi"}):
            reponse = chef.chat("Et l'agent emploi ?")
        self.assertIn("aaaaaaa", reponse["answer"])
        self.assertIn("bbbbbbb", reponse["answer"])
        self.assertNotIn("rnd_secret", reponse["answer"])

    def test_sans_cle_render(self):
        db.init()
        plan = {"action": "devops_deploiements", "agent_devops": "", "reponse": "", "a_retenir": []}
        with mock.patch.object(chef.llm, "ask_json", return_value=plan), \
                mock.patch.dict(os.environ, {"RENDER_API_KEY": ""}):
            self.assertIn("RENDER_API_KEY", chef.chat("Et maintenant ?")["answer"])


if __name__ == "__main__":
    unittest.main()
