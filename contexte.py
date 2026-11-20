"""Contexte transmis au modèle : souvenirs pertinents dans un budget, et conversation résumée par fil.

- memoire(texte, espace) : au lieu de toute la mémoire triée par importance, le socle (préférences de
  travail, souvenirs d'importance 5) puis les souvenirs qui partagent des mots avec la demande, puis les
  plus importants, jusqu'au budget. Recherche faite ici, en Python : identique sur Postgres et SQLite.
- conversation(fil) : résumé glissant des messages anciens du fil, puis ses derniers messages.
- resumer(fil) : met à jour ce résumé quand assez de messages sont sortis de la fenêtre récente.
"""
import json
import math
import re
import threading
import unicodedata

import db
import llm
import memory

MEMOIRE_BUDGET = 3000      # caractères de souvenirs dans une demande à l'agent chef
RECENTS = 10               # derniers messages du fil transmis tels quels
MESSAGE_MAX = 700          # caractères gardés par message récent
RESUME_LOT = 8             # nombre de messages sortis de la fenêtre qui déclenche une mise à jour du résumé
RESUME_MAX = 1500          # caractères du résumé
_resume_lock = threading.Lock()

MOTS_VIDES = set("""
alors au aucun aussi autre aux avec avoir bon car ce cela ces cet cette ceux chaque comme comment dans des
donc dont du elle elles en encore est et etait ete etre fait faire fois font hors ici il ils je juste la le
les leur leurs lui mais me meme mes moi mon ne nos notre nous on ou par parce pas peu peut plus pour pourquoi
qu quand que quel quelle quelles quels qui sa sans se ses seulement si sien son sont sous soyez sur ta tandis
te tes toi ton tous tout toute toutes tres tu un une vos votre vous vu ca cest the and for est sont avoir
veut veux voudrais peux dois doit merci bonjour stp svp
""".split())


def mots(texte):
    """Mots significatifs d'un texte : minuscules, sans accents, sans mots vides, réduits à leur racine
    approximative (pluriel retiré, 6 premières lettres)."""
    texte = unicodedata.normalize("NFKD", str(texte or "").lower())
    texte = "".join(c for c in texte if not unicodedata.combining(c))
    racines = []
    for m in re.findall(r"[a-z0-9]+", texte):
        if len(m) < 3 or m in MOTS_VIDES:
            continue
        if len(m) > 4 and m[-1] in "sx":
            m = m[:-1]
        racines.append(m[:6])
    return racines


def _socle(m):
    return m["category"] == "preferences" or int(m.get("importance") or 0) >= 5


def choisir(souvenirs, texte, budget=MEMOIRE_BUDGET):
    """Choisit les souvenirs à transmettre : socle, puis pertinents, puis importants, dans le budget."""
    demande = set(mots(texte))
    docs = [(m, set(mots(m["content"]))) for m in souvenirs]
    n = len(docs) or 1
    df = {}
    for _, d in docs:
        for w in d:
            df[w] = df.get(w, 0) + 1

    def score(item):
        m, d = item
        return sum(math.log(1 + n / df[w]) for w in d & demande)

    socle = [m for m, _ in docs if _socle(m)]
    pertinents = [m for m, d in sorted(docs, key=lambda x: (-score(x), -int(x[0]["importance"] or 0)))
                  if score((m, d)) > 0 and not _socle(m)]
    reste = [m for m, _ in sorted(docs, key=lambda x: -int(x[0]["importance"] or 0))]
    choisis, taille, vus = [], 0, set()
    for m in socle + pertinents + reste:
        if m["id"] in vus:
            continue
        cout = len(m["content"]) + 3
        if taille + cout > budget:
            continue
        choisis.append(m)
        vus.add(m["id"])
        taille += cout
    return choisis


def memoire(texte, espace=None, budget=MEMOIRE_BUDGET):
    """Bloc de souvenirs pour une demande. espace=None : toutes les catégories (agent chef)."""
    souvenirs = memory.recall(memory.categories_de(espace), limit=500)
    return memory.format_items(choisir(souvenirs, texte, budget))


# ---------- conversation : fils et résumé glissant ----------
def _cle(fil):
    return f"resume_fil:{fil}"


def resume(fil):
    try:
        etat = json.loads(db.kv_get(_cle(fil)) or "null")
    except ValueError:
        etat = None
    return etat if isinstance(etat, dict) else {"texte": "", "jusqu_a": 0}


def effacer_resume(fil):
    db.kv_set(_cle(fil), "null")


def _ligne(m, limite=MESSAGE_MAX):
    return f"{'Elle' if m['role'] == 'user' else 'Toi'} : {str(m['content'])[:limite]}"


def conversation(fil, recents=RECENTS):
    """Texte de la conversation pour le modèle : résumé des messages anciens, puis les derniers messages."""
    derniers = db.run("SELECT * FROM messages WHERE fil = ? ORDER BY id DESC LIMIT ?", (fil, recents))[::-1]
    texte = "\n".join(_ligne(m) for m in derniers) or "(début de conversation)"
    r = resume(fil)["texte"]
    return (f"Résumé des échanges plus anciens : {r}\n\n" if r else "") + texte


PROMPT_RESUME = """Tu tiens le résumé d'une conversation entre une personne et son agent chef (fil « {fil} »).
Mets à jour le résumé avec les nouveaux messages. Garde ce qui sert à comprendre la suite : demandes en
cours, décisions prises, réponses attendues, sujets abordés. Supprime ce qui est réglé ou anecdotique.
N'invente rien. N'y mets ni santé, ni opinions, ni données bancaires, ni identifiants ou mots de passe.
{max} caractères au plus.

RÉSUMÉ ACTUEL :
{resume}

NOUVEAUX MESSAGES (du plus ancien au plus récent) :
{messages}

Réponds en JSON : {{"resume": "..."}}"""


def resumer(fil, recents=RECENTS, lot=RESUME_LOT):
    """Intègre au résumé les messages sortis de la fenêtre récente, s'ils sont au moins `lot`.
    Renvoie True si le résumé a changé. Une erreur du modèle laisse le résumé tel quel."""
    if not _resume_lock.acquire(blocking=False):
        return False
    try:
        etat = resume(fil)
        fenetre = db.run("SELECT id FROM messages WHERE fil = ? ORDER BY id DESC LIMIT ?", (fil, recents))
        if len(fenetre) < recents:
            return False
        limite = fenetre[-1]["id"]   # premier message de la fenêtre récente
        anciens = db.run("SELECT * FROM messages WHERE fil = ? AND id > ? AND id < ? ORDER BY id",
                         (fil, etat["jusqu_a"], limite))
        if len(anciens) < lot:
            return False
        try:
            r = llm.ask_json(PROMPT_RESUME.format(fil=fil, max=RESUME_MAX, resume=etat["texte"] or "(vide)",
                                                  messages="\n".join(_ligne(m, 800) for m in anciens)))
        except llm.LLMError as e:
            print(f"Résumé du fil {fil} reporté ({e})", flush=True)
            return False
        texte = " ".join(str((r or {}).get("resume") if isinstance(r, dict) else "").split())[:RESUME_MAX]
        if not texte:
            return False
        db.kv_set(_cle(fil), json.dumps({"texte": texte, "jusqu_a": anciens[-1]["id"]}, ensure_ascii=False))
        return True
    finally:
        _resume_lock.release()


def resumer_plus_tard(fil):
    threading.Thread(target=resumer, args=(fil,), daemon=True).start()
