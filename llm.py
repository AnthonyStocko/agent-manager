"""Appels au modèle via Claude Code en mode non interactif (`claude -p`).

En local : la session Claude Code déjà connectée sur ta machine, donc ton abonnement.
Sur un serveur (Render) : le jeton d'abonnement créé sur ton PC avec `claude setup-token`,
placé dans la variable d'environnement CLAUDE_CODE_OAUTH_TOKEN.

Claude Code est lancé sans aucun outil (ni fichiers, ni commandes, ni web), dans un dossier
vide, et ne reçoit que les variables d'environnement de la liste ci-dessous : ni APP_PASSWORD
ni ANTHROPIC_API_KEY (sinon il utiliserait l'API payante) ne lui sont visibles.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile


CLI_TIMEOUT_SECONDS = int(os.getenv("CLI_TIMEOUT_SECONDS", "300"))
CLI_ENV_ALLOWLIST = [
    "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP",
    "USERPROFILE", "APPDATA", "LOCALAPPDATA", "SYSTEMROOT", "COMSPEC", "PATHEXT",  # Windows
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE",
    "CLAUDE_CODE_OAUTH_TOKEN",
]


class LLMError(Exception):
    pass


def _command():
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


def ask(prompt, timeout=None):
    command = _command()
    env = {k: os.environ[k] for k in CLI_ENV_ALLOWLIST if k in os.environ}
    workdir = tempfile.mkdtemp(prefix="agent-chef-")  # dossier vide : rien à lire
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


def ask_json(prompt, timeout=None):
    """Demande une réponse JSON et l'extrait même si elle est entourée de texte."""
    text = ask(prompt + "\n\nRéponds uniquement avec du JSON valide, sans texte autour.", timeout)
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
