# Agent chef (agent-manager)

Lire `README.md` et `ARCHITECTURE.md` avant toute modification : ils fixent le rôle de l'agent chef,
celui des autres agents et les frontières entre eux.

Règles non négociables :
- L'agent chef orchestre, il ne fait pas le travail des autres agents. Le code d'un autre agent n'est
  jamais copié ici : il reste dans son dépôt. Un nouvel agent a son propre dépôt et s'installe comme
  dépendance dans `requirements.txt`, à un commit précis, comme `agent-devops-perso` ; jamais comme
  module de l'agent chef.
- Confirmation avant écriture. Les demandes à agent-emploi (recherche et notation d'offres, CV et
  lettre, ajout d'une offre ou d'un CV) n'écrivent que dans sa propre base et partent sans confirmation,
  comme le point du matin (recherche, notation, mémoire). Tout le reste qui écrit attend un « oui » dans
  la discussion : chez agent-perso (agenda, tâches, dépenses), sur GitHub (agent-dev, création d'un
  agent) et sur Render (variables). Ces actions passent par `_set_pending`, affichent ce qui sera fait
  et n'agissent qu'à la confirmation, qui ne passe jamais par le modèle. Toute nouvelle action qui écrit
  hors de la base d'agent-emploi suit ce modèle (`devops_variable`, `dev_creer`).
- L'agent Instagram est en lecture seule depuis l'agent chef : requêtes GET uniquement sur son API,
  jamais de publication ni de validation d'un commentaire. Seule exception, qui ne passe pas par son
  API : l'agent devops peut lancer un workflow GitHub de sa liste blanche (aujourd'hui
  `deploy-supabase.yml` du dépôt de l'agent Instagram), après un « oui », sur le modèle de
  `devops_variable`.
- La mémoire commune ne retient jamais : santé, opinions politiques ou religieuses, origine, vie
  intime, données bancaires, identifiants, mots de passe, ni détails sur des tiers nommés (consigne de
  `memory.py`). Ne pas affaiblir cette liste.
- Accès : les pages et `/api/` exigent une session ; les routes des autres agents exigent `HUB_TOKEN` ;
  le point du matin exige `CRON_SECRET`. Sur Render, sans `APP_PASSWORD`, l'appli refuse toute demande
  (réponse 503).
- Aucun secret (jeton, clé, mot de passe) dans le dépôt, dans une réponse, dans un message d'erreur ni
  dans un journal : les jetons sont masqués (`***`) avant affichage.
- Toute modification de qui appelle qui, ou de ce qu'un agent a le droit d'écrire, met à jour
  `ARCHITECTURE.md`.
- Avant de terminer : `pip install -r requirements.txt`, puis
  `python -m unittest discover -p 'test_*.py'`.
