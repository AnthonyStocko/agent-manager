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
| agent Instagram Mon guide | lien vers son administration | pas encore relié |

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

Depuis l'onglet **Développeur** ou la discussion (« dans l'agent emploi, trie les offres par date »,
puis « oui »), tu demandes une modification du code d'un de ces dépôts : agent-emploi, agent-perso,
agent-manager, mon-guide-agent-instagram, Monguide.

Le travail ne tourne pas sur Render mais dans GitHub Actions (workflow `developpeur.yml` de ce
dépôt, gratuit car le dépôt est public), en 5 à 20 minutes :

1. Claude Code (ton abonnement) lit le dépôt visé et fait la modification, tests compris.
2. Les vérifications du dépôt sont lancées ; si elles échouent, Claude Code corrige (deux essais).
3. Une branche `dev/<référence>` et une pull request sont créées. Si tout est vert, la pull request
   est fusionnée dans `main` et le déploiement habituel du dépôt suit, **sans relecture**.
4. Le compte rendu arrive dans la discussion et dans l'onglet Développeur.

Garde-fous, réglés par dépôt dans `developpeur/depots.json` :

| Dépôt | Vérifications avant fusion | Jamais modifié | Pull request laissée à relire |
|---|---|---|---|
| agent-emploi | compilation, tests s'il y en a, démarrage du serveur | `.github/` | — |
| agent-perso | compilation, tous les tests `test_*.py` | `.github/` | — |
| agent-manager | compilation, tests, démarrage du serveur | `.github/`, `developpeur/` | — |
| mon-guide-agent-instagram | `verifier-principes.mjs`, puis la CI du dépôt | `.github/`, `CLAUDE.md`, `PRINCIPES.md`, `verifier-principes.mjs` | migrations Supabase |
| Monguide | `npm test`, puis les workflows du dépôt | `.github/` | migrations et rollbacks Supabase |

- Un travail qui touche un chemin « jamais modifié » est rejeté en entier, rien n'est poussé.
- Vérifications en échec : la pull request reste ouverte, préfixée « [tests en échec] », rien n'est fusionné.
- agent-emploi n'a pas encore de tests : « vert » y veut seulement dire que le code compile et que
  le serveur démarre. L'agent développeur a pour consigne d'ajouter des tests à chaque modification.
- Une seule demande à la fois par dépôt ; les suivantes attendent.
- Claude Code ne reçoit ni le jeton GitHub ni `HUB_TOKEN` ; il ne peut ni pousser ni fusionner lui-même.

### Mise en place (une fois)

1. **Jeton GitHub** : GitHub > Settings > Developer settings > Fine-grained tokens > Generate new
   token. Repository access : les cinq dépôts ci-dessus. Permissions : **Contents** (Read and
   write), **Pull requests** (Read and write), **Actions** (Read and write).
2. **Secrets de ce dépôt** (Settings > Secrets and variables > Actions) : `DEV_GITHUB_TOKEN` (le
   jeton de l'étape 1), `CLAUDE_CODE_OAUTH_TOKEN` (le même que sur Render), `HUB_TOKEN` (la même
   valeur que sur Render) ; `AGENT_URL` existe déjà pour le point du matin.
3. **Render** (agent-manager) : ajoute la variable `DEV_GITHUB_TOKEN` avec le même jeton.

Le workflow peut aussi être lancé à la main : onglet Actions > Agent développeur > Run workflow.

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
   - `INSTAGRAM_URL` : facultatif.
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
- Autres variables : `PERSO_PROVIDER` (modèle demandé à agent-perso, `claude-pro` par défaut),
  `CLAUDE_CLI_MODEL`, `CLI_TIMEOUT_SECONDS`.

## Fichiers

- `app.py` : serveur, connexion, routes — `chef.py` : discussion, tableau de bord, point du matin
- `memory.py` : mémoire et extraction des souvenirs — `agents.py` : connexion aux agents
- `dev.py` : demandes à l'agent développeur — `developpeur/` : son moteur (workflow GitHub Actions)
- `files.py` : pièces jointes — `db.py` : Postgres ou SQLite — `llm.py` : appel à Claude Code — `static/` : interface
