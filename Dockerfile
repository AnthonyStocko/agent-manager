# Image Python + Node.js : Node.js est nécessaire pour Claude Code
FROM python:3.12-slim

COPY --from=node:22-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force

# Utilisateur sans droits administrateur (son dossier personnel accueille ~/.claude).
# Le dossier de l'application lui appartient pour qu'il puisse y écrire en local.
RUN useradd --create-home app && mkdir -p /home/app/src && chown app:app /home/app/src
WORKDIR /home/app/src

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app . .
USER app

ENV PYTHONUNBUFFERED=1
# Render fournit le port dans la variable PORT et définit RENDER=true (écoute sur 0.0.0.0)
CMD ["python", "app.py"]
