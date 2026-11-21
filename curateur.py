"""Curateur : surveille la consommation du modèle, allège le contexte et entretient la mémoire.

Trois rôles, qui ne changent ni les droits des agents ni ce que la mémoire accepte de retenir :

1. Métriques : lecture des appels enregistrés par llm.py (jetons, taille du contexte, latence par agent).
2. Avant l'appel au modèle (chemin de la réponse) : `preparer` choisit les souvenirs utiles, retire ce qui
   est déjà dans le prompt ou la conversation, et tronque l'historique au budget de l'agent. Tout est
   déterministe, sans aucun appel au modèle.
3. Après la réponse, jamais pendant : `distiller` transforme les échanges anciens en faits atomiques avec
   un modèle économique ; `nettoyer_memoire` fusionne les doublons et archive l'obsolète, sans modèle.
   L'archivage ne supprime rien : le souvenir reçoit une date de fin de validité et reste dans le journal.
"""
import datetime
import json
import re
import threading

import agents
import contexte
import db
import llm
import memory

CARACTERES_PAR_JETON = 4
PART_MEMOIRE = 0.4            # part maximale du budget pour les souvenirs
HISTORIQUE_MINIMUM = 1500     # caractères d'historique gardés même si le budget est serré
SEUIL_DEDOUBLONNAGE = 0.8     # un souvenir dont 80 % des mots sont déjà dans le prompt est retiré
SEUIL_DOUBLON = 0.9           # deux souvenirs dont 90 % des mots se recoupent sont des doublons
DISTILLATION_SEUIL = 20       # anciens messages non distillés qui déclenchent la distillation
MESSAGES_CONSERVES = 60       # messages bruts toujours gardés dans un fil (ceux que la page affiche)
ARCHIVE_JOURS = 180           # sans usage ni modification depuis ce délai, un souvenir peu important est archivé
ARCHIVE_IMPORTANCE_MAX = 2
METRIQUES_JOURS = 30
_distillation_lock = threading.Lock()

# Mots-clés par espace de mémoire : l'intention détectée dans la demande, sans modèle.
INTENTIONS = {
    "emploi": "offre offres emploi candidature candidater postuler cv lettre entretien recruteur poste salaire "
              "recherche relance annonce annonces",
    "perso": "mail mails agenda tache taches depense depenses planning rendez-vous brouillon calendrier "
             "reunion rappel journee",
    "mon_guide": "instagram publication commentaire commentaires legende post mon guide veille abonnes",
    "dev": "code modifier modification bug depot agent deploiement deployer render github test tests "
           "fonction architecture variable",
}
_MOTS_INTENTION = {espace: set(contexte.mots(texte)) for espace, texte in INTENTIONS.items()}
# Consignes durables d'un message : à garder dans l'historique même s'il est ancien.
CONTRAINTE = re.compile(r"(?<!\w)(toujours|jamais|surtout|uniquement|seulement|interdit|obligatoire|"
                        r"d[ée]sormais|[àa] partir de maintenant|[ée]vite[rz]?|pas de|n'?oublie)(?!\w)"
                        r"|(?<!\w)ne\s+\w+\s+(?:pas|plus|jamais)(?!\w)", re.I)


def budget_caracteres(agent):
    return agents.budget_tokens(agent) * CARACTERES_PAR_JETON


# ---------- 2. avant l'appel : contexte filtré, dédupliqué, tronqué ----------
def intention(texte):
    """Espaces de mémoire que la demande évoque (mots-clés), sans modèle."""
    m = set(contexte.mots(texte))
    return {espace for espace, cles in _MOTS_INTENTION.items() if m & cles}


def _inclus(a, b):
    """Part des mots de a qui sont aussi dans b."""
    return len(a & b) / len(a) if a else 0.0


def _protege(m):
    return contexte._socle(m) or int(m.get("priority") or 0) >= 1


def filtrer_memoire(souvenirs, texte, espace=None, reference=""):
    """Garde les souvenirs utiles à la demande : le socle et les prioritaires, ceux qui partagent des mots
    avec la demande, ceux de l'espace évoqué (intention ou fil). Retire ensuite les doublons entre eux et
    ce que `reference` (prompt système, derniers messages) dit déjà. Les protégés ne sont jamais retirés
    à cause de `reference`."""
    demande = set(contexte.mots(texte))
    espaces = intention(texte) | ({espace} if espace else set())
    deja = set(contexte.mots(reference))
    gardes, vus = [], []
    for m in souvenirs:
        mots = set(contexte.mots(m["content"]))
        propre = memory.CATEGORIE_ESPACE.get(m["category"])
        if not (_protege(m) or mots & demande or (propre != "commun" and propre in espaces)):
            continue
        if any(_inclus(mots, v) >= SEUIL_DOUBLON and _inclus(v, mots) >= SEUIL_DOUBLON for v in vus):
            continue
        if not _protege(m) and len(mots) >= 3 and _inclus(mots, deja) >= SEUIL_DEDOUBLONNAGE:
            continue
        vus.append(mots)
        gardes.append(m)
    return gardes


def tronquer_historique(messages, budget, resume=""):
    """Texte de la conversation dans `budget` caractères. Garde toujours la dernière demande de la personne,
    puis les messages qui posent une consigne durable (« toujours », « jamais »…), puis les plus récents.
    `messages` : dictionnaires role/content, du plus ancien au plus récent."""
    lignes = [contexte._ligne(m) for m in messages]
    resume_txt = f"Résumé des échanges plus anciens : {resume}\n\n" if resume else ""
    if resume_txt and len(resume_txt) > budget * 0.3:
        resume_txt = resume_txt[:max(0, int(budget * 0.3) - 4)].rstrip() + "…\n\n"
    reste = max(budget - len(resume_txt), 0)
    derniere = next((i for i in range(len(messages) - 1, -1, -1) if messages[i]["role"] == "user"), None)
    choisis, taille = set(), 0
    ordre = ([derniere] if derniere is not None else [])
    ordre += [i for i in range(len(messages) - 1, -1, -1)
              if i != derniere and messages[i]["role"] == "user" and CONTRAINTE.search(str(messages[i]["content"]))]
    ordre += [i for i in range(len(messages) - 1, -1, -1) if i not in ordre]
    for i in ordre:
        cout = len(lignes[i]) + 1
        if taille + cout > reste:
            if i == derniere:   # la dernière demande reste, raccourcie s'il le faut
                lignes[i] = lignes[i][:max(reste - 1, 0)]
                choisis.add(i)
                taille += len(lignes[i]) + 1
            continue
        choisis.add(i)
        taille += cout
    corps = "\n".join(lignes[i] for i in sorted(choisis)) or "(début de conversation)"
    return resume_txt + corps


def preparer(agent, message, messages, fil, espace=None, etat="", prompt_statique=""):
    """Contexte variable d'un appel au modèle, dans le budget de l'agent. Renvoie un dictionnaire :
    memoire, historique, taille (caractères injectés), brut (taille sans optimisation), ids (souvenirs transmis).
    Déterministe : aucun appel au modèle."""
    budget = budget_caracteres(agent)
    recents = " ".join(m["content"] for m in messages if m["role"] == "user")[-1500:]
    souvenirs = memory.recall(memory.categories_de(espace), limit=500)
    brut_souvenirs = contexte.choisir(souvenirs, message + " " + recents)
    brut_memoire = memory.format_items(brut_souvenirs)
    reference = prompt_statique + "\n" + "\n".join(str(m["content"]) for m in messages)
    utiles = filtrer_memoire(souvenirs, message + " " + recents, espace, reference)
    part = min(contexte.MEMOIRE_BUDGET, int(budget * PART_MEMOIRE))
    choisis = contexte.choisir(utiles, message + " " + recents, part)
    texte_memoire = memory.format_items(choisis)
    resume = contexte.resume(fil)["texte"]
    place = max(budget - len(texte_memoire) - len(etat), HISTORIQUE_MINIMUM)
    historique = tronquer_historique(messages, place, resume)
    brut_historique = (f"Résumé des échanges plus anciens : {resume}\n\n" if resume else "") + (
        "\n".join(contexte._ligne(m) for m in messages) or "(début de conversation)")
    return {"memoire": texte_memoire, "historique": historique,
            "taille": len(texte_memoire) + len(historique) + len(etat),
            "brut": len(brut_memoire) + len(brut_historique) + len(etat),
            "ids": [m["id"] for m in choisis]}


def marquer_utilises_plus_tard(ids):
    """Met à jour last_accessed et access_count hors du chemin de la réponse."""
    def tache():
        try:
            memory.marquer_utilises(ids)
        except Exception as e:
            print(f"Usage de la mémoire non enregistré ({e!r})", flush=True)
    if ids:
        threading.Thread(target=tache, daemon=True).start()


# ---------- 1. métriques ----------
def rapport(jours=7):
    """Consommation par agent sur les `jours` derniers jours, et ratio de réduction du contexte."""
    depuis = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=jours)).isoformat(
        timespec="seconds")
    par_agent, total_ctx, total_brut = [], 0, 0
    for r in db.run("SELECT agent_name, COUNT(*) AS appels, SUM(prompt_tokens) AS entree, "
                    "SUM(completion_tokens) AS sortie, SUM(context_size_chars) AS contexte, "
                    "SUM(context_raw_chars) AS brut, AVG(latency_ms) AS latence, MAX(latency_ms) AS latence_max "
                    "FROM llm_metrics WHERE timestamp >= ? GROUP BY agent_name ORDER BY agent_name", (depuis,)):
        contexte_total, brut = int(r["contexte"] or 0), int(r["brut"] or 0)
        total_ctx, total_brut = total_ctx + contexte_total, total_brut + brut
        par_agent.append({
            "agent": r["agent_name"], "appels": int(r["appels"]), "tokens_entree": int(r["entree"] or 0),
            "tokens_sortie": int(r["sortie"] or 0), "contexte_moyen_chars": round(contexte_total / int(r["appels"])),
            "latence_moyenne_ms": round(float(r["latence"] or 0)), "latence_max_ms": int(r["latence_max"] or 0),
            "ratio_reduction": round(1 - contexte_total / brut, 3) if brut else 0.0})
    return {"jours": jours, "agents": par_agent,
            "budgets": {k: agents.budget_tokens(k) for k in agents.BUDGETS_TOKENS},
            "ratio_reduction": round(1 - total_ctx / total_brut, 3) if total_brut else 0.0}


# ---------- 3. après la réponse : distillation et hygiène ----------
def _cle_distille(fil):
    return f"distille_fil:{fil}"


PROMPT_DISTILLATION = """Tu distilles d'anciens échanges entre une personne et son agent chef en faits atomiques
durables pour sa mémoire à long terme.

CATÉGORIES possibles :
{categories}

MÉMOIRE DÉJÀ RETENUE (ne la répète pas) :
{memoires}

ÉCHANGES ANCIENS (du plus ancien au plus récent) :
{messages}

RÈGLES :
- Un fait = une phrase autonome, précise, à la 3e personne (« Il… »). Rien si rien n'est durable : renvoie [].
- Ne retiens que ce que la personne a dit d'elle-même, de ses goûts, objectifs ou façons de travailler.
- N'invente rien. Ne retiens jamais : santé, opinions politiques ou religieuses, origine, vie intime,
  données bancaires, identifiants, mots de passe, ni détails sur des tiers nommés.
- importance : 1 (anecdotique) à 5 (essentiel). Un fait temporaire reçoit "valable_jusqu_au" (AAAA-MM-JJ) ;
  aujourd'hui : {today}.

Réponds par une liste JSON : [{{"category": "...", "content": "...", "importance": 3, "valable_jusqu_au": null}}]"""


def _anciens(fil, etat_distille):
    """Messages du fil sortis de la fenêtre récente et pas encore distillés."""
    fenetre = db.run("SELECT id FROM messages WHERE fil = ? ORDER BY id DESC LIMIT ?", (fil, contexte.RECENTS))
    if len(fenetre) < contexte.RECENTS:
        return []
    return db.run("SELECT * FROM messages WHERE fil = ? AND id > ? AND id < ? ORDER BY id",
                  (fil, etat_distille, fenetre[-1]["id"]))


def distiller(fil, seuil=DISTILLATION_SEUIL):
    """Transforme les anciens échanges du fil en souvenirs atomiques, puis purge les messages bruts devenus
    redondants (déjà résumés, déjà distillés, hors des MESSAGES_CONSERVES derniers). À appeler après la
    réponse, jamais avant. Renvoie {"faits": n, "purges": n} ; une erreur du modèle ne change rien."""
    resultat = {"faits": 0, "purges": 0}
    if not _distillation_lock.acquire(blocking=False):
        return resultat
    try:
        deja = int(db.kv_get(_cle_distille(fil), "0") or 0)
        anciens = _anciens(fil, deja)
        if len(anciens) < seuil:
            return resultat
        autorisees = memory.categories_ecrites_par("chef")
        existants = memory.recall(autorisees, limit=80)
        prompt = PROMPT_DISTILLATION.format(
            categories="\n".join(f"- {k} : {memory.CATEGORIES[k]}" for k in autorisees),
            memoires="\n".join(f"- {m['content']}" for m in existants) or "(vide)",
            messages="\n".join(contexte._ligne(m, 800) for m in anciens if m["role"] == "user")[:12000],
            today=memory._today())
        try:
            with llm.suivi("curateur"):
                faits = llm.ask_json_economique(prompt)
        except llm.LLMError as e:
            print(f"Distillation du fil {fil} reportée ({e})", flush=True)
            return resultat
        connus = [set(contexte.mots(m["content"])) for m in existants]
        for f in faits if isinstance(faits, list) else []:
            try:
                mots = set(contexte.mots(f.get("content")))
                if not mots or any(_inclus(mots, c) >= SEUIL_DOUBLON for c in connus):
                    continue
                memory.add(f.get("category"), f.get("content"), "curateur", f.get("importance"),
                           valid_until=f.get("valable_jusqu_au"))
                connus.append(mots)
                resultat["faits"] += 1
            except (memory.SensitiveError, AttributeError, TypeError, ValueError):
                continue   # contenu refusé par la mémoire ou mal formé : ignoré, rien n'est conservé
        db.kv_set(_cle_distille(fil), str(anciens[-1]["id"]))
        resultat["purges"] = _purger_messages(fil)
        return resultat
    finally:
        _distillation_lock.release()


def _purger_messages(fil):
    """Efface les messages bruts déjà résumés ET distillés, au-delà des MESSAGES_CONSERVES derniers."""
    limite = min(int(db.kv_get(_cle_distille(fil), "0") or 0), contexte.resume(fil)["jusqu_a"])
    gardes = db.run("SELECT id FROM messages WHERE fil = ? ORDER BY id DESC LIMIT ?", (fil, MESSAGES_CONSERVES))
    if limite <= 0 or len(gardes) < MESSAGES_CONSERVES:
        return 0
    borne = min(limite + 1, gardes[-1]["id"])
    n = db.run("SELECT COUNT(*) AS n FROM messages WHERE fil = ? AND id < ?", (fil, borne))[0]["n"]
    db.run("DELETE FROM messages WHERE fil = ? AND id < ?", (fil, borne))
    return n


def distiller_plus_tard(fil):
    threading.Thread(target=_distiller_sans_erreur, args=(fil,), daemon=True).start()


def _distiller_sans_erreur(fil):
    try:
        distiller(fil)
    except Exception as e:   # tâche de fond : elle ne doit jamais gêner l'appli
        print(f"Distillation du fil {fil} en erreur ({e!r})", flush=True)


def _derniere_activite(m):
    return max(str(m.get(k) or "") for k in ("last_accessed", "confirmed_at", "updated_at", "created_at"))


def nettoyer_memoire(maintenant=None):
    """Fusionne les doublons d'une même catégorie et archive les souvenirs peu importants sans usage depuis
    ARCHIVE_JOURS jours. Sans modèle. Tout passe par memory.update/delete : le journal permet d'annuler.
    Renvoie {"fusions": n, "archives": n, "metriques_purgees": n}."""
    maintenant = maintenant or datetime.datetime.now(datetime.timezone.utc)
    resultat = {"fusions": 0, "archives": 0, "metriques_purgees": 0}
    souvenirs = memory.recall(limit=100000)
    mots = {m["id"]: set(contexte.mots(m["content"])) for m in souvenirs}
    # on garde le plus prioritaire, puis le plus important, le plus consulté, le plus récent
    souvenirs.sort(key=_derniere_activite, reverse=True)
    souvenirs.sort(key=lambda m: (-int(m.get("priority") or 0), -int(m["importance"] or 0),
                                  -int(m.get("access_count") or 0)))
    disparus = set()
    for i, garde in enumerate(souvenirs):
        if garde["id"] in disparus:
            continue
        for autre in souvenirs[i + 1:]:
            if autre["id"] in disparus or autre["category"] != garde["category"]:
                continue
            a, b = mots[garde["id"]], mots[autre["id"]]
            if len(a) < 3 or len(b) < 3 or _inclus(a, b) < SEUIL_DOUBLON or _inclus(b, a) < SEUIL_DOUBLON:
                continue
            db.run("UPDATE memories SET access_count = ?, priority = ?, last_accessed = ? WHERE id = ?",
                   (int(garde.get("access_count") or 0) + int(autre.get("access_count") or 0),
                    max(int(garde.get("priority") or 0), int(autre.get("priority") or 0)),
                    max(str(garde.get("last_accessed") or ""), str(autre.get("last_accessed") or "")) or None,
                    garde["id"]))
            if memory.delete(autre["id"], "curateur"):
                disparus.add(autre["id"])
                resultat["fusions"] += 1
    limite = (maintenant - datetime.timedelta(days=ARCHIVE_JOURS)).isoformat(timespec="seconds")
    hier = (maintenant.date() - datetime.timedelta(days=1)).isoformat()
    for m in memory.recall(limit=100000):
        if m["id"] in disparus or _protege(m) or int(m["importance"] or 0) > ARCHIVE_IMPORTANCE_MAX:
            continue
        try:
            if _derniere_activite(m) < limite and memory.update(m["id"], source="curateur", valid_until=hier):
                resultat["archives"] += 1
        except ValueError:
            continue   # souvenir ancien que le filtre actuel refuserait : on n'y touche pas
    purge = (maintenant - datetime.timedelta(days=METRIQUES_JOURS)).isoformat(timespec="seconds")
    resultat["metriques_purgees"] = db.run("SELECT COUNT(*) AS n FROM llm_metrics WHERE timestamp < ?", (purge,))[0]["n"]
    db.run("DELETE FROM llm_metrics WHERE timestamp < ?", (purge,))
    return resultat


def compacter_tout(fils):
    """Distillation de chaque fil puis hygiène de la mémoire (point du matin, tâche de fond)."""
    bilan = {"faits": 0, "purges": 0}
    for fil in fils:
        r = distiller(fil)
        bilan = {k: bilan[k] + r[k] for k in bilan}
    return {**bilan, **nettoyer_memoire()}
