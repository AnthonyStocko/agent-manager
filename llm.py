"""Appels aux modèles de langage (LLM) : Google Gemini et secours Claude Code.

Principal : API Google Gemini via le SDK officiel `google-genai` (modèle `gemini-2.5-flash` par défaut).
Nécessite la variable d'environnement GEMINI_API_KEY (avec support de python-dotenv en local).

Secours (optionnel) : Claude Code en mode non interactif (`claude -p`) si GEMINI_API_KEY n'est pas
définie et qu'une session ou le jeton CLAUDE_CODE_OAUTH_TOKEN est présent.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
AVAILABLE_MODELS = [
    {"id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash (rapide, recommandé)", "provider": "gemini"},
    {"id": "gemini-2.5-pro", "name": "Gemini 2.5 Pro (raisonnement)", "provider": "gemini"},
    {"id": "gemini-1.5-flash", "name": "Gemini 1.5 Flash", "provider": "gemini"},
    {"id": "gemini-1.5-pro", "name": "Gemini 1.5 Pro", "provider": "gemini"},
    {"id": "claude", "name": "Claude Code (CLI local)", "provider": "claude"},
]

CLI_TIMEOUT_SECONDS = int(os.getenv("CLI_TIMEOUT_SECONDS", "300"))
CLI_ENV_ALLOWLIST = [
    "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP",
    "USERPROFILE", "APPDATA", "LOCALAPPDATA", "SYSTEMROOT", "COMSPEC", "PATHEXT",  # Windows
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE",
    "CLAUDE_CODE_OAUTH_TOKEN",
]

_gemini_client = None
_gemini_cached_key = None


class LLMError(Exception):
    pass


def is_gemini_configured():
    return bool((os.getenv("GEMINI_API_KEY") or "").strip())


def get_gemini_client():
    global _gemini_client, _gemini_cached_key
    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not api_key:
        raise LLMError("Variable d'environnement GEMINI_API_KEY manquante. "
                       "Renseigne-la dans ton environnement ou dans un fichier .env.")
    try:
        from google import genai
    except ImportError:
        raise LLMError("SDK google-genai introuvable. Lance `pip install -r requirements.txt`.")
    if _gemini_client is None or _gemini_cached_key != api_key:
        _gemini_client = genai.Client(api_key=api_key)
        _gemini_cached_key = api_key
    return _gemini_client


def ask_gemini(prompt, model=None, timeout=None, json_mode=False):
    """Appel à l'API Gemini via le SDK officiel google-genai."""
    client = get_gemini_client()
    target_model = model or os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
    try:
        from google.genai import types
        config_kwargs = {}
        if json_mode:
            config_kwargs["response_mime_type"] = "application/json"
        if timeout:
            config_kwargs["http_options"] = types.HttpOptions(timeout=float(timeout))
        config = types.GenerateContentConfig(**config_kwargs) if config_kwargs else None

        response = client.models.generate_content(
            model=target_model,
            contents=prompt,
            config=config,
        )
    except Exception as e:
        raise LLMError(f"Erreur API Gemini ({target_model}) : {e}")

    text = response.text or ""
    return text.strip()


def _claude_command():
    exe = shutil.which("claude")
    if not exe:
        raise LLMError("Claude Code est introuvable. En local : installe-le et lance `claude` une "
                       "fois pour te connecter. Sur Render : déploie avec le Dockerfile.")
    token = (os.getenv("CLAUDE_CODE_OAUTH_TOKEN") or "").strip()
    if os.getenv("RENDER") and not token:
        raise LLMError("Renseigne CLAUDE_CODE_OAUTH_TOKEN (obtenu avec `claude setup-token`) "
                       "dans les variables d'environnement Render.")
    if token.startswith("sk-ant-api"):
        raise LLMError("CLAUDE_CODE_OAUTH_TOKEN contient une clé API (sk-ant-api…), facturée au "
                       "crédit. Mets le jeton d'abonnement (sk-ant-oat01-…) de `claude setup-token`.")
    command = [exe, "-p", "--tools", "", "--strict-mcp-config", "--output-format", "json"]
    if os.getenv("CLAUDE_CLI_MODEL"):
        command += ["--model", os.getenv("CLAUDE_CLI_MODEL")]
    return command


def ask_claude(prompt, timeout=None):
    """Appel de secours via Claude Code CLI (`claude -p`)."""
    command = _claude_command()
    env = {k: os.environ[k] for k in CLI_ENV_ALLOWLIST if k in os.environ}
    workdir = tempfile.mkdtemp(prefix="agent-chef-")
    try:
        proc = subprocess.run(command, input=prompt, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout or CLI_TIMEOUT_SECONDS, cwd=workdir, env=env)
    except subprocess.TimeoutExpired:
        raise LLMError("Claude Code n'a pas répondu à temps. Réessaie.")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    out = (proc.stdout or "").strip()
    try:
        data = json.loads(out)
    except ValueError:
        data = {"is_error": True, "result": out or (proc.stderr or "").strip()}
    if not isinstance(data, dict):
        data = {"is_error": True, "result": out}
    message = " ".join(str(data.get("result") or "").split())
    if data.get("is_error") or proc.returncode != 0:
        print(f"Erreur Claude Code (code {proc.returncode}) : {message[-500:]!r}", flush=True)
        if "limit" in message.lower():
            raise LLMError("Limite d'usage de l'abonnement atteinte : " + message[:200])
        raise LLMError("Erreur Claude Code : " + (message[:300] or str(proc.returncode)))
    return str(data.get("result") or "").strip()


def ask(prompt, model=None, timeout=None):
    """Envoie un prompt au modèle (spécifié ou par défaut)."""
    if model == "claude" or (not model and not is_gemini_configured() and (os.getenv("CLAUDE_CODE_OAUTH_TOKEN") or shutil.which("claude"))):
        return ask_claude(prompt, timeout=timeout)
    if is_gemini_configured() or model:
        return ask_gemini(prompt, model=model, timeout=timeout, json_mode=False)
    raise LLMError("Variable d'environnement GEMINI_API_KEY manquante. "
                   "Renseigne-la dans ton environnement ou dans un fichier .env.")


def ask_json(prompt, model=None, timeout=None):
    """Demande une réponse JSON et l'extrait même si elle est entourée de texte."""
    if model == "claude" or (not model and not is_gemini_configured() and (os.getenv("CLAUDE_CODE_OAUTH_TOKEN") or shutil.which("claude"))):
        text = ask_claude(prompt + "\n\nRéponds uniquement avec du JSON valide, sans texte autour.", timeout)
    elif is_gemini_configured() or model:
        text = ask_gemini(prompt + "\n\nRéponds uniquement avec du JSON valide, sans texte autour.",
                          model=model, timeout=timeout, json_mode=True)
    else:
        raise LLMError("Variable d'environnement GEMINI_API_KEY manquante. "
                       "Renseigne-la dans ton environnement ou dans un fichier .env.")

    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                continue
    raise LLMError("Réponse du modèle illisible (JSON attendu).")
