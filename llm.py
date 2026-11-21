"""Appels aux modèles de langage (LLM) : Google Gemini et secours Claude Code.

Principal : API Google Gemini via le SDK officiel `google-genai` (modèle `gemini-2.5-flash` par défaut).
Nécessite la variable d'environnement GEMINI_API_KEY (avec support de python-dotenv en local).
Utilise l'API v1beta pour assurer la compatibilité avec le mode JSON.

Secours (optionnel) : Claude Code en mode non interactif (`claude -p`) si GEMINI_API_KEY n'est pas
définie et qu'une session ou le jeton CLAUDE_CODE_OAUTH_TOKEN est présent.
"""
import contextlib
import contextvars
import json
import math
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time

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

# Modèle rapide et économique, réservé aux tâches de fond (distillation de la mémoire, curateur.py).
# Sans clé Gemini, ces tâches utilisent le modèle par défaut (secours Claude Code).
MODELE_ECONOMIQUE = os.getenv("GEMINI_MODEL_ECONOMIQUE", "gemini-2.5-flash-lite")

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


# ---------- métriques : une ligne par appel, écrite hors du chemin de la réponse ----------
# Désactivées tant qu'on ne les active pas (activer_metriques, appelée au démarrage de l'appli) : les
# outils et les tests qui importent ce module n'écrivent rien dans la base.
_metriques = {"actives": False}
_file = queue.Queue(maxsize=1000)
_travailleur = {"thread": None}
_travailleur_lock = threading.Lock()
_suivi_courant = contextvars.ContextVar("suivi_llm", default=None)
_usage = threading.local()   # jetons rapportés par le fournisseur pour l'appel en cours


def activer_metriques(actives=True):
    _metriques["actives"] = bool(actives)


def estimer_tokens(texte):
    """Estimation sans modèle : environ 4 caractères par jeton."""
    return math.ceil(len(str(texte or "")) / 4)


@contextlib.contextmanager
def suivi(agent, contexte_chars=None, brut_chars=None):
    """Étiquette les appels du bloc : agent, taille du contexte injecté, et taille qu'il aurait eue sans
    optimisation (pour le ratio de réduction)."""
    jeton = _suivi_courant.set({"agent": agent, "contexte": contexte_chars, "brut": brut_chars})
    try:
        yield
    finally:
        _suivi_courant.reset(jeton)


def _ecrire_metrique(m):
    import db
    db.run("INSERT INTO llm_metrics(agent_name, model, prompt_tokens, completion_tokens, context_size_chars, "
           "context_raw_chars, latency_ms, timestamp) VALUES(?,?,?,?,?,?,?,?)",
           (m["agent_name"], m["model"], m["prompt_tokens"], m["completion_tokens"], m["context_size_chars"],
            m["context_raw_chars"], m["latency_ms"], m["timestamp"]))


def _boucle_ecriture():
    while True:
        m = _file.get()
        try:
            _ecrire_metrique(m)
        except Exception as e:   # une métrique perdue ne doit jamais gêner un appel
            print(f"Métrique non enregistrée ({e!r})", flush=True)
        finally:
            _file.task_done()


def enregistrer_metrique(agent_name, model, prompt_tokens, completion_tokens, context_size_chars,
                         latency_ms, context_raw_chars=None):
    """Met la métrique en file ; un thread l'écrit dans la base. Ne bloque jamais : file pleine, elle est perdue."""
    if not _metriques["actives"]:
        return False
    import db
    m = {"agent_name": str(agent_name or "inconnu")[:40], "model": str(model or "")[:60],
         "prompt_tokens": int(prompt_tokens or 0), "completion_tokens": int(completion_tokens or 0),
         "context_size_chars": int(context_size_chars or 0),
         "context_raw_chars": int(context_raw_chars if context_raw_chars is not None else context_size_chars or 0),
         "latency_ms": int(latency_ms or 0), "timestamp": db.now()}
    with _travailleur_lock:
        if _travailleur["thread"] is None:
            _travailleur["thread"] = threading.Thread(target=_boucle_ecriture, daemon=True)
            _travailleur["thread"].start()
    try:
        _file.put_nowait(m)
    except queue.Full:
        return False
    return True


def attendre_metriques():
    """Attend que la file soit écrite (tests, arrêt propre)."""
    _file.join()


def _mesurer(prompt, model, appel):
    """Exécute l'appel au modèle et enregistre ses métriques. L'appel réussi seulement."""
    _usage.valeur = None
    debut = time.monotonic()
    texte = appel()
    if _metriques["actives"]:
        ms = int((time.monotonic() - debut) * 1000)
        entree, sortie = getattr(_usage, "valeur", None) or (estimer_tokens(prompt), estimer_tokens(texte))
        s = _suivi_courant.get() or {}
        taille = s.get("contexte") if s.get("contexte") is not None else len(prompt)
        enregistrer_metrique(s.get("agent") or "inconnu", model or os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL),
                             entree, sortie, taille, ms, s.get("brut"))
    return texte


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
        from google.genai import types
    except ImportError:
        raise LLMError("SDK google-genai introuvable. Lance `pip install -r requirements.txt`.")
    if _gemini_client is None or _gemini_cached_key != api_key:
        http_options = types.HttpOptions(api_version="v1beta")
        _gemini_client = genai.Client(api_key=api_key, http_options=http_options)
        _gemini_cached_key = api_key
    return _gemini_client


def ask_gemini(prompt, model=None, timeout=None, json_mode=False):
    """Appel à l'API Gemini (v1beta) via le SDK officiel google-genai."""
    client = get_gemini_client()
    target_model = model or os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
    try:
        from google.genai import types
        config_kwargs = {}
        if json_mode:
            config_kwargs["response_mime_type"] = "application/json"
        http_kwargs = {"api_version": "v1beta"}
        if timeout:
            http_kwargs["timeout"] = float(timeout)
        config_kwargs["http_options"] = types.HttpOptions(**http_kwargs)
        config = types.GenerateContentConfig(**config_kwargs)

        response = client.models.generate_content(
            model=target_model,
            contents=prompt,
            config=config,
        )
    except Exception as e:
        raise LLMError(f"Erreur API Gemini ({target_model}) : {e}")

    meta = getattr(response, "usage_metadata", None)
    entree, sortie = getattr(meta, "prompt_token_count", None), getattr(meta, "candidates_token_count", None)
    if isinstance(entree, int) and isinstance(sortie, int):
        _usage.valeur = (entree, sortie)
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
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    if isinstance(usage.get("input_tokens"), int) and isinstance(usage.get("output_tokens"), int):
        _usage.valeur = (usage["input_tokens"] + int(usage.get("cache_read_input_tokens") or 0)
                         + int(usage.get("cache_creation_input_tokens") or 0), usage["output_tokens"])
    message = " ".join(str(data.get("result") or "").split())
    if data.get("is_error") or proc.returncode != 0:
        print(f"Erreur Claude Code (code {proc.returncode}) : {message[-500:]!r}", flush=True)
        if "limit" in message.lower():
            raise LLMError("Limite d'usage de l'abonnement atteinte : " + message[:200])
        raise LLMError("Erreur Claude Code : " + (message[:300] or str(proc.returncode)))
    return str(data.get("result") or "").strip()


def ask(prompt, model=None, timeout=None):
    """Envoie un prompt au modèle (spécifié ou par défaut)."""
    return _mesurer(prompt, model, lambda: _ask(prompt, model, timeout))


def _ask(prompt, model, timeout):
    if model == "claude" or (not model and not is_gemini_configured() and shutil.which("claude")):
        return ask_claude(prompt, timeout=timeout)
    if is_gemini_configured() or model:
        return ask_gemini(prompt, model=model, timeout=timeout, json_mode=False)
    raise LLMError("Variable d'environnement GEMINI_API_KEY manquante. "
                   "Renseigne-la dans ton environnement ou dans un fichier .env.")


def ask_json(prompt, model=None, timeout=None):
    """Demande une réponse JSON et l'extrait même si elle est entourée de texte."""
    return _mesurer(prompt, model, lambda: _ask_json_texte(prompt, model, timeout))


def ask_json_economique(prompt, timeout=None):
    """Comme ask_json, avec le modèle rapide et économique (tâches de fond, jamais la réponse à la personne)."""
    return ask_json(prompt, model=MODELE_ECONOMIQUE if is_gemini_configured() else None, timeout=timeout)


def _ask_json_texte(prompt, model, timeout):
    if model == "claude" or (not model and not is_gemini_configured() and shutil.which("claude")):
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
