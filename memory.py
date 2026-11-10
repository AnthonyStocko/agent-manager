"""Mémoire commune : les agents envoient des événements, l'agent chef en tire des souvenirs."""
import threading

import db
import llm

CATEGORIES = {
    "profil": "Qui tu es : métier, situation, objectifs",
    "gouts_offres": "Tes goûts en offres d'emploi",
    "style_ecriture": "Ton style d'écriture",
    "recits": "Tes récits d'expérience réutilisables",
    "points_a_travailler": "Tes points à travailler en entretien",
    "preferences": "Comment tu veux que tes agents travaillent",
}
MAX_MEMORIES_IN_PROMPT = 150
_processing = threading.Lock()


def recall(categories=None, limit=60):
    cats = [c for c in (categories or CATEGORIES) if c in CATEGORIES]
    if not cats:
        return []
    marks = ", ".join("?" * len(cats))
    return db.run(f"SELECT * FROM memories WHERE category IN ({marks}) "
                  "ORDER BY importance DESC, updated_at DESC LIMIT ?", (*cats, int(limit)))


def as_text(categories=None, limit=60):
    """Bloc de texte à glisser dans le prompt d'un agent."""
    lines, current = [], None
    for m in sorted(recall(categories, limit), key=lambda m: list(CATEGORIES).index(m["category"])):
        if m["category"] != current:
            current = m["category"]
            lines.append(f"{CATEGORIES[current]} :")
        lines.append(f"- {m['content']}")
    return "\n".join(lines)


def add(category, content, source="manuel", importance=3):
    content = " ".join(str(content or "").split())[:600]
    if category not in CATEGORIES or not content:
        raise ValueError("Souvenir invalide.")
    importance = max(1, min(5, int(importance or 3)))
    return db.run("INSERT INTO memories(category, content, source, importance, created_at, updated_at) "
                  "VALUES(?,?,?,?,?,?) RETURNING id",
                  (category, content, source, importance, db.now(), db.now()))[0]["id"]


def update(mid, content=None, category=None, importance=None):
    rows = db.run("SELECT * FROM memories WHERE id = ?", (mid,))
    if not rows:
        return False
    m = rows[0]
    content = " ".join(str(content if content is not None else m["content"]).split())[:600]
    category = category if category in CATEGORIES else m["category"]
    importance = max(1, min(5, int(importance or m["importance"])))
    db.run("UPDATE memories SET content=?, category=?, importance=?, updated_at=? WHERE id=?",
           (content, category, importance, db.now(), mid))
    return True


def delete(mid):
    db.run("DELETE FROM memories WHERE id = ?", (mid,))


def add_event(agent, kind, content):
    """Enregistre un événement et lance son traitement en arrière-plan."""
    content = str(content or "").strip()[:8000]
    if not content:
        return
    db.run("INSERT INTO events(agent, kind, content, created_at) VALUES(?,?,?,?)",
           (str(agent)[:40], str(kind)[:60], content, db.now()))
    threading.Thread(target=process_pending, daemon=True).start()


def pending_count():
    return db.run("SELECT COUNT(*) AS n FROM events WHERE processed = 0")[0]["n"]


PROMPT = """Tu tiens la mémoire à long terme d'une personne, partagée entre ses agents IA (recherche
d'emploi, assistant personnel, etc.). À partir des nouveaux événements, décide ce qui doit changer.

CATÉGORIES :
{categories}

MÉMOIRE ACTUELLE :
{memories}

NOUVEAUX ÉVÉNEMENTS :
{events}

RÈGLES :
- Ne retiens que ce qui est durable et utile pour mieux servir cette personne plus tard. La plupart
  des événements ne méritent aucun souvenir : renvoie alors [].
- Un souvenir = une phrase autonome, précise, à la 3e personne (« Il… »), compréhensible sans contexte.
- Ne duplique pas : si un souvenir existant dit déjà la même chose, renforce-le ou précise-le avec
  « update ». Si un événement le contredit, corrige-le. Supprime ce qui est devenu faux.
- Ne généralise pas à partir d'un seul cas. Une offre écartée une fois donne « A écarté une offre
  de … (raison probable : …) », pas « Ne veut pas de … ». Passe à une règle générale seulement
  quand plusieurs événements vont dans le même sens, et monte alors l'importance.
- Style d'écriture : compare le texte avant et après correction et retiens la règle (ton, longueur,
  formules supprimées ou ajoutées), pas le contenu.
- Récits : retiens les situations concrètes racontées en entretien (contexte, action, résultat).
- N'invente rien. Ne retiens jamais : santé, opinions politiques ou religieuses, origine, vie
  intime, données bancaires, identifiants, mots de passe, ni détails sur des tiers nommés.
- importance : 1 (anecdotique) à 5 (essentiel).

Format : liste JSON d'opérations, parmi
{{"op": "add", "category": "...", "content": "...", "importance": 3}}
{{"op": "update", "id": 12, "content": "...", "importance": 4}}
{{"op": "delete", "id": 12}}"""


def process_pending():
    """Transforme les événements en attente en souvenirs. Renvoie le nombre d'opérations appliquées."""
    if not _processing.acquire(blocking=False):
        return 0
    applied = 0
    try:
        while True:
            events = db.run("SELECT * FROM events WHERE processed = 0 ORDER BY id LIMIT 8")
            if not events:
                break
            existing = recall(limit=MAX_MEMORIES_IN_PROMPT)
            prompt = PROMPT.format(
                categories="\n".join(f"- {k} : {v}" for k, v in CATEGORIES.items()),
                memories="\n".join(f"[{m['id']}] ({m['category']}, importance {m['importance']}) "
                                   f"{m['content']}" for m in existing) or "(vide)",
                events="\n\n".join(f"--- Événement de l'agent « {e['agent']} », type « {e['kind']} », "
                                   f"le {e['created_at'][:10]} ---\n{e['content'][:3500]}" for e in events))
            ids = [e["id"] for e in events]
            marks = ", ".join("?" * len(ids))
            try:
                ops = llm.ask_json(prompt)
            except llm.LLMError as e:
                print(f"Mémoire : traitement reporté ({e})", flush=True)
                # Après 3 échecs, l'événement est abandonné pour ne pas bloquer la file
                db.run(f"UPDATE events SET attempts = attempts + 1 WHERE id IN ({marks})", ids)
                db.run("UPDATE events SET processed = 1 WHERE processed = 0 AND attempts >= 3")
                break
            known = {m["id"] for m in existing}
            source = ", ".join(sorted({e["agent"] for e in events}))
            for op in ops if isinstance(ops, list) else []:
                try:
                    kind = op.get("op")
                    if kind == "add":
                        add(op.get("category"), op.get("content"), source, op.get("importance"))
                    elif kind == "update" and int(op.get("id")) in known:
                        update(int(op["id"]), op.get("content"), None, op.get("importance"))
                    elif kind == "delete" and int(op.get("id")) in known:
                        delete(int(op["id"]))
                    else:
                        continue
                    applied += 1
                except (AttributeError, TypeError, ValueError):
                    continue
            db.run(f"UPDATE events SET processed = 1 WHERE id IN ({marks})", ids)
    finally:
        _processing.release()
    return applied
