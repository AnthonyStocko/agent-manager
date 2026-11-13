# Agent chef (agent-manager)

L'agent qui chapeaute les autres. Cinq rôles :

- **Mémoire commune** : il retient ce que tes agents apprennent de toi (goûts en offres, style
  d'écriture, récits d'expérience, points à travailler, profil, préférences) dans une base
  Postgres. Chaque agent y lit avant de travailler et y envoie ce qu'il observe.
- **Point d'entrée unique** : tu lui parles, il répond lui-même ou confie la demande au bon agent.
- **Tableau de bord** : l'état de tous tes agents sur une page.
- **Initiatives** : un point du matin (recherche d'offres, notation, relances) lancé sans clic.
- **Agent développeur** : tu décris une amélioration, il modifie le code de l'agent concerné, le
  teste et le met en ligne (voir plus bas).

## Agents chapeautés

| Agent | Ce que l'agent chef sait faire | État |
|---|---|---|
| agent-emploi | mémoire dans les deux sens, recherche, candidature, résumé, point du matin | complet (branche `connexion-agent-chef` d'agent-emploi) |
| agent-perso | lui transmettre toute demande par sa zone de saisie unique (`/assistant`) : dépenses, tâches, mails, agenda, planning ; confirmation par « oui » / « non » avant toute écriture | sans modification d'agent-perso ; pas encore de mémoire partagée |
| agent Instagram Mon guide | consulter les commentaires en attente (lecture seule : discussion « quels commentaires attendent ? » et carte du tableau de bord), lien vers son administration | lecture seule ; validation dans l'agent Instagram |

## Pièces jointes et photos

Dans la discussion, le trombone joint un fichier et l'appareil photo prend une photo (sur
téléphone). Selon ta demande, l'agent chef en fait :

- **une dépense** : photo d'un ticket, lue par agent-perso, enregistrée après ton « oui » ;
  une photo envoyée sans un mot est traitée comme un ticket ;
- **une pièce jointe de mail** : le document part dans le brouillon préparé par agent-perso ;
- **un CV** ajouté à ton profil dans agent-emploi (PDF, DOCX, ODT, TXT) ;
- **une offre d'emploi** ajoutée à ton suivi (document texte, pas une photo).

Les fichiers ne sont jamais écrits sur le disque : ils restent en mémoire 15 minutes au plus, le
temps de la confirmation. Limite : 10 Mo ; les photos sont réduites avant l'envoi.

## Agent développeur

L'agent développeur a son propre dépôt, [agent-dev](https://github.com/AnthonyStocko/agent-dev) :
il modifie le code d'agent-emploi, agent-perso, agent-manager, de l'agent Instagram et de Mon guide,
le teste dans GitHub Actions et le fusionne dans `main` si tout est vert, sans relecture. Ses
garde-fous et sa mise en place sont décrits dans son README.

L'agent chef le commande :

- **onglet Développeur** : choix de l'agent, description de la modification, suivi des demandes
  (en cours, fusionnée, à relire, en échec) avec les liens vers la pull request et le journal ;
- **discussion** : « dans l'agent emploi, trie les offres par date », puis « oui » pour lancer ;
- le compte rendu de l'agent développeur arrive dans la discussion à la fin du travail.

### Créer un nouvel agent

« Crée un agent qui surveille tels sites et m'envoie un résumé » : si le rôle n'est pas clair, l'agent
chef pose la question. Sinon il affiche un récapitulatif (hébergement, libellé, description) et attend
**oui** ou **non**. Le routeur renvoie un champ `hebergement` : `chef` par défaut, `depot` seulement si
tu demandes explicitement un agent dans son propre dépôt.

**Par défaut (`chef`) : l'agent est un module de l'agent chef.** Récapitulatif : « module de l'agent
chef ». Aucun dépôt n'est créé, `depots.json` n'est pas modifié. Après « oui », une seule demande de
développement part sur agent-manager (`dev.submit`) : l'agent développeur crée, sur le modèle de
`devops.py`, un fichier `<nom_du_module>.py` à la racine (clé avec tirets remplacés par des soulignés,
bibliothèque standard autant que possible), une nouvelle action dans le routeur de `chef.py` (réponse
affichée dans la discussion), des tests `test_<nom_du_module>.py` avec services externes simulés, une
section du README et, si besoin, les variables d'environnement dans `render.yaml`, sans toucher aux
agents et actions existants. La clé est refusée si `<nom_du_module>.py` existe déjà à la racine.

**Avec `depot` : agent dans son propre dépôt** (récapitulatif « dépôt privé AnthonyStocko/<clé> »).
Après « oui », trois étapes dans cet ordre :

1. création du dépôt GitHub privé et vide (`POST /user/repos`, avec `DEV_GITHUB_TOKEN`) ;
2. ajout de l'agent au fichier `depots.json` d'agent-dev (branche `main`) ;
3. demande de développement sur ce dépôt : Python sans dépendance inutile, `app.py` qui répond sur
   `/healthz`, tests `test_*.py`, README listant les variables d'environnement.

Le compte rendu arrive dans la discussion à la fin du travail. Garde-fous : clé refusée si elle n'a pas
la forme `agent-xxx` (minuscules, chiffres, tirets), si elle existe déjà dans la liste ou si elle vaut
`agent-dev` ; si le dépôt existe déjà sur GitHub (message explicite, lu dans le tableau `errors` de la réponse de GitHub) ou si une étape échoue, tout s'arrête et le message
dit quelles étapes ont réussi et laquelle a échoué (jamais le jeton). Aucun dépôt existant n'est
modifié ni supprimé. Le jeton `DEV_GITHUB_TOKEN` doit avoir le droit de créer des dépôts (jeton
classique : portée `repo` ; jeton à granularité fine : « Administration » en écriture sur le compte).

Configuration côté agent chef : la variable Render `DEV_GITHUB_TOKEN` (le jeton GitHub créé pour
agent-dev, voir son README). `HUB_TOKEN` doit avoir la même valeur dans les secrets d'agent-dev.

## Devops : vérifier les déploiements

« Est-ce que le correctif de l'agent emploi est en ligne ? » dans la discussion, ou le bouton
**Vérifier les déploiements** de l'onglet Développeur : pour l'agent chef, l'agent emploi et l'agent
perso, le commit en ligne sur Render est comparé au dernier commit de `main` sur GitHub.

| Verdict | Sens |
|---|---|
| ✅ à jour | le dernier commit GitHub est en ligne |
| ⚠️ commit différent | ce qui est en ligne n'est pas le dernier commit |
| ❌ échec | le dernier déploiement a échoué (le commit resté en ligne est indiqué) |
| ⏳ en cours | un déploiement est en cours |

Lecture seule : rien n'est modifié sur Render ni sur GitHub. L'agent développeur (GitHub Actions) et
l'agent Instagram (Supabase) ne sont pas sur Render et ne sont pas suivis.

Variables Render de l'agent chef : `RENDER_API_KEY` (Account Settings > API Keys ; elle donne accès à
tout le compte Render, garde-la uniquement ici), `RENDER_SERVICE_AGENT_EMPLOI` et
`RENDER_SERVICE_AGENT_PERSO` (identifiants `srv-…`, en haut de la page de chaque service). Le service
de l'agent chef est reconnu tout seul, et GitHub est lu avec `DEV_GITHUB_TOKEN`.

## Comment la mémoire s'enrichit

1. Un agent signale un événement : offre écartée ou retenue, texte corrigé à la main, entretien
   simulé terminé, profil importé, ou une information que tu donnes dans la discussion.
2. L'agent chef compare l'événement à la mémoire existante et ajoute, précise, corrige ou supprime
   des souvenirs. Il ne généralise pas à partir d'un seul cas et ne retient rien de sensible
   (santé, opinions, finances, identifiants, tiers).
3. Avant de noter une offre, de rédiger ou de préparer un entretien, agent-emploi relit les
   souvenirs utiles.

Tout est visible et modifiable dans l'onglet Mémoire.

## Déploiement

1. **Base Postgres** : crée une base gratuite (Neon ou Supabase) et copie son URL de connexion
   (`postgresql://…`).
2. **Render** : New > Blueprint, choisis ce dépôt, puis renseigne :
   - `APP_PASSWORD` : mot de passe de l'agent chef (identifiant `admin`).
   - `CLAUDE_CODE_OAUTH_TOKEN` : le même jeton d'abonnement que tes autres agents.
   - `DATABASE_URL` : l'URL de l'étape 1. Sans elle, la mémoire part dans un fichier local,
     effacé à chaque redéploiement.
   - `HUB_TOKEN` : une longue chaîne aléatoire de ton choix.
   - `CRON_SECRET` : une autre chaîne aléatoire.
   - `EMPLOI_URL` : l'adresse d'agent-emploi (`https://….onrender.com`).
   - `PERSO_URL` et `PERSO_PASSWORD` : l'adresse d'agent-perso et son `APP_PASSWORD`.
   - `INSTAGRAM_URL` : facultatif (adresse de l'agent Instagram). Pour consulter ses commentaires en
     attente, ajoute aussi `INSTAGRAM_TOKEN` (jeton d'accès qu'il accepte en `Authorization: Bearer`) ;
     `INSTAGRAM_COMMENTS_PATH` (`api/comments/pending` par défaut) est la route qui les liste.
3. **agent-emploi** : fusionne sa branche `connexion-agent-chef`, puis ajoute dans ses variables
   Render `HUB_URL` (l'adresse de l'agent chef) et `HUB_TOKEN` (la même valeur qu'ici).
4. **Point du matin** : dans ce dépôt GitHub, Settings > Secrets and variables > Actions, ajoute
   `AGENT_URL` (l'adresse de l'agent chef) et `CRON_SECRET`. Le workflow tourne du lundi au vendredi.

## En local

    pip install -r requirements.txt   # seulement si tu utilises Postgres
    python app.py                     # http://127.0.0.1:8770, base SQLite locale

## À savoir

- Chaque message de discussion et chaque lot d'événements consomme un appel à Claude Code sur
  ton abonnement.
- Sur l'offre gratuite de Render, un agent endormi met environ une minute à se réveiller : la
  première demande de la journée est lente.
- `HUB_TOKEN` donne accès à la mémoire et à l'API d'agent-emploi : traite-le comme un mot de passe.
- Autre variable de l'agent développeur : `DEV_REPO` (son dépôt, `AnthonyStocko/agent-dev` par défaut).
- Autres variables : `PERSO_PROVIDER` (modèle demandé à agent-perso, `claude-pro` par défaut),
  `CLAUDE_CLI_MODEL`, `CLI_TIMEOUT_SECONDS`.

## Fichiers

- `app.py` : serveur, connexion, routes — `chef.py` : discussion, tableau de bord, point du matin
- `memory.py` : mémoire et extraction des souvenirs — `agents.py` : connexion aux agents
- `dev.py` : commande de l'agent développeur (dépôt agent-dev)
- `devops.py` : vérification des déploiements Render
- `files.py` : pièces jointes — `db.py` : Postgres ou SQLite — `llm.py` : appel à Claude Code — `static/` : interface
