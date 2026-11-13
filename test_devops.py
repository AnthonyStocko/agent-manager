"""Tests de la vérification des déploiements (devops.py), avec Render et GitHub simulés.

Lancement : python -m unittest test_devops -v
"""
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="test-chef-"))
os.environ.pop("DATABASE_URL", None)

import app  # noqa: E402
import chef  # noqa: E402
import db  # noqa: E402
import devops  # noqa: E402

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
            if url.startswith(devops.RENDER_API):
                service = url.split("/services/")[1].split("/")[0]
                if service not in self.render:
                    raise devops.DevopsError("HTTP 404")
                return self.render[service]
            depot = url.split("/repos/")[1].split("/commits/")[0]
            return {"sha": self.github[depot], "commit": {"message": "Correctif"}}

        env = {"RENDER_API_KEY": "rnd_secret", "RENDER_SERVICE_ID": "srv-chef", "RENDER_SERVICE_AGENT_MANAGER": "",
               "RENDER_SERVICE_AGENT_EMPLOI": "srv-emploi", "RENDER_SERVICE_AGENT_PERSO": "srv-perso",
               "GITHUB_TOKEN": "", "DEV_GITHUB_TOKEN": "ghp_dev"}
        for p in (mock.patch.object(devops, "_get", faux_get), mock.patch.dict(os.environ, env)):
            p.start()
            self.addCleanup(p.stop)

    def verdicts(self, **kw):
        return {a["agent"]: a["verdict"] for a in devops.deployments(**kw)["agents"]}

    def test_tout_a_jour(self):
        r = devops.deployments()
        self.assertTrue(r["tout_est_a_jour"])
        self.assertEqual([a["agent"] for a in r["agents"]], ["manager", "emploi", "perso"])
        self.assertIn("tout est à jour", devops.text(r))
        # Render est lu avec sa clé, GitHub avec le jeton de l'agent développeur
        self.assertEqual({t for u, t in self.appels if u.startswith(devops.RENDER_API)}, {"rnd_secret"})
        self.assertEqual({t for u, t in self.appels if u.startswith(devops.GITHUB_API)}, {"ghp_dev"})

    def test_commit_pas_encore_en_ligne(self):
        self.github["AnthonyStocko/agent-emploi"] = SHA_B
        r = devops.deployments()
        self.assertFalse(r["tout_est_a_jour"])
        self.assertEqual(self.verdicts(), {"manager": "a_jour", "emploi": "commit_different", "perso": "a_jour"})
        self.assertIn("aaaaaaa", devops.text(r))
        self.assertIn("bbbbbbb", devops.text(r))

    def test_echec_et_deploiement_en_cours(self):
        self.render["srv-emploi"] = [deploy(SHA_B, "build_failed"), deploy(SHA_A, "live")]
        self.render["srv-perso"] = [deploy(SHA_B, "build_in_progress"), deploy(SHA_A, "live")]
        r = devops.deployments()
        self.assertEqual(self.verdicts(), {"manager": "a_jour", "emploi": "echec", "perso": "en_cours"})
        emploi = r["agents"][1]
        self.assertIn("Toujours en ligne : aaaaaaa", emploi["resume"])
        self.assertEqual(emploi["en_ligne"]["commit"], SHA_A)

    def test_commit_precis_et_agent_seul(self):
        self.assertEqual(self.verdicts(agent="perso", commit="aaaaaaa"), {"perso": "a_jour"})
        self.assertEqual(self.verdicts(agent="perso", commit="bbbbbbb"), {"perso": "commit_different"})
        self.assertFalse(any(u.startswith(devops.GITHUB_API) for u, _ in self.appels))
        with self.assertRaises(devops.DevopsError):
            devops.deployments(agent="instagram")

    def test_service_non_configure_ou_en_erreur(self):
        self.render.pop("srv-perso")
        with mock.patch.dict(os.environ, {"RENDER_SERVICE_AGENT_EMPLOI": ""}):
            r = devops.deployments()
        self.assertEqual({a["agent"]: a["verdict"] for a in r["agents"]},
                         {"manager": "a_jour", "emploi": "non_configure", "perso": "erreur"})
        self.assertIn("RENDER_SERVICE_AGENT_EMPLOI", r["agents"][1]["resume"])
        self.assertNotIn("rnd_secret", str(r))

    def test_sans_cle_render(self):
        with mock.patch.dict(os.environ, {"RENDER_API_KEY": ""}), self.assertRaises(devops.DevopsError) as ctx:
            devops.deployments()
        self.assertIn("RENDER_API_KEY", str(ctx.exception))

    def test_reponse_render_sans_enveloppe(self):
        self.render["srv-emploi"] = [{"commit": {"id": SHA_A}, "status": "live"}]
        self.assertEqual(self.verdicts(agent="emploi"), {"emploi": "a_jour"})

    def test_route_et_discussion(self):
        r = app.api_get("/api/devops/deploiements", {"agent": ["emploi"]})
        self.assertEqual([a["agent"] for a in r["agents"]], ["emploi"])
        db.init()
        plan = {"action": "devops_deploiements", "agent_devops": "", "reponse": "", "a_retenir": []}
        with mock.patch.object(chef.llm, "ask_json", return_value=plan):
            reponse = chef.chat("Est-ce que mes agents sont bien déployés ?")
        self.assertIn("tout est à jour", reponse["answer"])
        self.assertEqual(chef.history()[-1]["agent"], "devops")
        with mock.patch.object(chef.llm, "ask_json", return_value=plan), \
                mock.patch.dict(os.environ, {"RENDER_API_KEY": ""}):
            self.assertIn("RENDER_API_KEY", chef.chat("Et maintenant ?")["answer"])


if __name__ == "__main__":
    unittest.main()
