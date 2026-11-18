import json
import os
import unittest
from unittest import mock

import llm
import chef
import app


class GeminiLLMTest(unittest.TestCase):
    def setUp(self):
        # Reset cached client
        llm._gemini_client = None
        llm._gemini_cached_key = None

    def test_gemini_not_configured_raises(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False):
            self.assertFalse(llm.is_gemini_configured())
            with mock.patch("shutil.which", return_value=None):
                with self.assertRaises(llm.LLMError) as ctx:
                    llm.ask("Bonjour")
                self.assertIn("GEMINI_API_KEY manquante", str(ctx.exception))

    def test_gemini_configured_detected(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key-123"}):
            self.assertTrue(llm.is_gemini_configured())

    def test_get_gemini_client_configures_v1beta(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key-123"}):
            with mock.patch("google.genai.Client") as mock_client_cls:
                llm.get_gemini_client()
                mock_client_cls.assert_called_once()
                call_kwargs = mock_client_cls.call_args.kwargs
                self.assertEqual(call_kwargs["api_key"], "test-key-123")
                self.assertEqual(call_kwargs["http_options"].api_version, "v1beta")

    def test_ask_gemini_success(self):
        mock_response = mock.MagicMock()
        mock_response.text = "Réponse Gemini"
        mock_client = mock.MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key-123", "GEMINI_MODEL": "gemini-2.5-flash"}):
            with mock.patch("llm.get_gemini_client", return_value=mock_client):
                res = llm.ask("Bonjour")
                self.assertEqual(res, "Réponse Gemini")
                mock_client.models.generate_content.assert_called_once()
                call_kwargs = mock_client.models.generate_content.call_args.kwargs
                self.assertEqual(call_kwargs["model"], "gemini-2.5-flash")
                self.assertEqual(call_kwargs["contents"], "Bonjour")
                self.assertEqual(call_kwargs["config"].http_options.api_version, "v1beta")

    def test_ask_gemini_custom_model(self):
        mock_response = mock.MagicMock()
        mock_response.text = "Réponse Gemini Pro"
        mock_client = mock.MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key-123"}):
            with mock.patch("llm.get_gemini_client", return_value=mock_client):
                res = llm.ask("Bonjour", model="gemini-2.5-pro")
                self.assertEqual(res, "Réponse Gemini Pro")
                call_kwargs = mock_client.models.generate_content.call_args.kwargs
                self.assertEqual(call_kwargs["model"], "gemini-2.5-pro")
                self.assertEqual(call_kwargs["config"].http_options.api_version, "v1beta")

    def test_ask_json_gemini_success(self):
        mock_response = mock.MagicMock()
        mock_response.text = '{"action": "repondre", "reponse": "Bonjour !"}'
        mock_client = mock.MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key-123"}):
            with mock.patch("llm.get_gemini_client", return_value=mock_client):
                res = llm.ask_json("Dis bonjour en JSON")
                self.assertEqual(res, {"action": "repondre", "reponse": "Bonjour !"})
                call_kwargs = mock_client.models.generate_content.call_args.kwargs
                self.assertEqual(call_kwargs["config"].response_mime_type, "application/json")
                self.assertEqual(call_kwargs["config"].http_options.api_version, "v1beta")

    def test_ask_json_custom_model(self):
        mock_response = mock.MagicMock()
        mock_response.text = '{"action": "repondre", "reponse": "Ok Pro"}'
        mock_client = mock.MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key-123"}):
            with mock.patch("llm.get_gemini_client", return_value=mock_client):
                res = llm.ask_json("test", model="gemini-2.5-pro")
                self.assertEqual(res, {"action": "repondre", "reponse": "Ok Pro"})
                call_kwargs = mock_client.models.generate_content.call_args.kwargs
                self.assertEqual(call_kwargs["model"], "gemini-2.5-pro")
                self.assertEqual(call_kwargs["config"].http_options.api_version, "v1beta")

    def test_ask_json_strips_markdown_fences(self):
        mock_response = mock.MagicMock()
        mock_response.text = '```json\n{"action": "repondre", "reponse": "Ok"}\n```'
        mock_client = mock.MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key-123"}):
            with mock.patch("llm.get_gemini_client", return_value=mock_client):
                res = llm.ask_json("test")
                self.assertEqual(res, {"action": "repondre", "reponse": "Ok"})

    def test_ask_gemini_error_handling(self):
        mock_client = mock.MagicMock()
        mock_client.models.generate_content.side_effect = Exception("Erreur réseau API Gemini")

        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key-123"}):
            with mock.patch("llm.get_gemini_client", return_value=mock_client):
                with self.assertRaises(llm.LLMError) as ctx:
                    llm.ask("test")
                self.assertIn("Erreur API Gemini", str(ctx.exception))

    def test_ask_claude_when_model_is_claude(self):
        with mock.patch("llm.ask_claude", return_value="Réponse Claude") as mock_claude:
            res = llm.ask("test", model="claude")
            self.assertEqual(res, "Réponse Claude")
            mock_claude.assert_called_once_with("test", timeout=None)

    def test_chef_chat_entry_point_uses_custom_model(self):
        plan = {
            "action": "repondre",
            "reponse": "Salut ! Réponse Pro",
            "a_retenir": []
        }
        mock_response = mock.MagicMock()
        mock_response.text = json.dumps(plan)
        mock_client = mock.MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key-123"}):
            with mock.patch("llm.get_gemini_client", return_value=mock_client):
                result = chef.chat("Salut l'agent chef", model="gemini-2.5-pro")
                self.assertIn("Salut !", result["answer"])
                mock_client.models.generate_content.assert_called_once()
                call_kwargs = mock_client.models.generate_content.call_args.kwargs
                self.assertEqual(call_kwargs["model"], "gemini-2.5-pro")
                self.assertEqual(call_kwargs["config"].http_options.api_version, "v1beta")

    def test_api_messages_returns_available_models(self):
        with mock.patch("chef.history", return_value=[]), mock.patch("dev.sync"):
            res = app.api_get("/api/messages", {})
            self.assertIn("models", res)
            self.assertIn("default_model", res)
            model_ids = [m["id"] for m in res["models"]]
            self.assertIn("gemini-2.5-flash", model_ids)
            self.assertIn("gemini-2.5-pro", model_ids)
            self.assertIn("claude", model_ids)


if __name__ == "__main__":
    unittest.main()
