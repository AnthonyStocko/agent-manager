"""Mémoire commune : les agents envoient des événements, l'agent chef en tire des souvenirs.

Toute écriture passe par add, update ou delete, qui :
- refusent un contenu sensible (sensitive_reason), quelle que soit son origine, modèle ou saisie manuelle ;
- tiennent le journal des changements (table memory_log), d'où chaque changement peut être annulé (undo).
Le journal garde l'avant et l'après pendant JOURNAL_DAYS jours, puis efface ces contenus.
"""
import datetime
import json
import re
import threading

import db
import llm

# La mémoire est découpée en espaces. Tous les agents lisent « commun » ; chacun lit en plus le sien.
# Un événement envoyé par un agent ne peut écrire que dans « commun » et dans son espace (AGENT_ESPACE).
# Les données de tiers (contacts, rendez-vous, dépenses) restent chez agent-perso : elles n'ont pas de
# catégorie ici.
ESPACES = {
    "commun": "Commun à tous tes agents",
    "emploi": "Recherche d'emploi",
    "perso": "Assistant personnel",
    "mon_guide": "Mon guide",
    "dev": "Développement de tes agents",
}
CATEGORIES = {
    "profil": "Qui tu es : métier, situation, objectifs",
    "preferences": "Comment tu veux que tes agents travaillent",
    "style_ecriture": "Ton style d'écriture",
    "gouts_offres": "Tes goûts en offres d'emploi",
    "recits": "Tes récits d'expérience réutilisables",
    "points_a_travailler": "Tes points à travailler en entretien",
    "habitudes": "Tes habitudes d'organisation : rythme, horaires, façon de planifier",
    "ligne_editoriale": "Ligne éditoriale de Mon guide : ton, sujets, ce que tu acceptes ou refuses",
    "decisions_techniques": "Décisions techniques sur tes agents : architecture, choix, règles",
}
CATEGORIE_ESPACE = {
    "profil": "commun", "preferences": "commun", "style_ecriture": "commun",
    "gouts_offres": "emploi", "recits": "emploi", "points_a_travailler": "emploi",
    "habitudes": "perso",
    "ligne_editoriale": "mon_guide",
    "decisions_techniques": "dev",
}
# Agent qui envoie l'événement -> espace où il a le droit d'écrire (en plus de « commun »).
# L'agent chef (discussion) et la saisie manuelle écrivent partout ; un agent inconnu, dans « commun » seulement.
AGENT_ESPACE = {"emploi": "emploi", "perso": "perso", "instagram": "mon_guide", "dev": "dev", "devops": "dev"}
PARTOUT = ("chef", "manuel")


def categories_de(espace=None):
    """Catégories lues par un agent de cet espace : « commun » et les siennes. Sans espace : toutes."""
    if not espace:
        return list(CATEGORIES)
    if espace not in ESPACES:
        raise ValueError("Espace de mémoire inconnu : " + ", ".join(ESPACES) + ".")
    return [c for c in CATEGORIES if CATEGORIE_ESPACE[c] in ("commun", espace)]


def categories_ecrites_par(agent):
    """Catégories qu'un événement de cet agent a le droit de créer, modifier ou supprimer."""
    if agent in PARTOUT:
        return list(CATEGORIES)
    return categories_de(AGENT_ESPACE.get(agent)) if agent in AGENT_ESPACE else categories_de("commun")
MAX_MEMORIES_IN_PROMPT = 150
JOURNAL_DAYS = 30
_processing = threading.Lock()


# ---------- données sensibles : jamais retenues, contrôlées ici et pas seulement dans la consigne ----------
class SensitiveError(ValueError):
    """Contenu refusé parce qu'il touche une catégorie que la mémoire ne retient jamais."""


_SENSIBLE = [
    ("données bancaires", r"\bFR\d{2}(?:\s?[0-9A-Z]{4}){5}\s?[0-9A-Z]{3}\b|\b[A-Z]{2}\d{2}(?:\s?[0-9A-Z]{4}){3,7}\b"
                          r"|\b(?:iban|bic|rib|num[ée]ro de (?:carte|compte)|cryptogramme|code pin)\b"),
    ("identifiants", r"\b(?:mot de passe|mots de passe|password|passwd|mdp|identifiant de connexion|code secret)\b"
                     r"|\b(?:ghp_|gho_|github_pat_|sk-|sk_live_|rnd_|xox[abp]-|AIza)[A-Za-z0-9_\-]{8,}"),
    ("santé", r"\b(?:maladie|malade|diagnostic|diagnostiqu\w*|sympt[ôo]mes?|m[ée]dicaments?|traitement m[ée]dical"
              r"|ordonnance|hospitalis\w*|chirurgi\w*|handicap\w*|cancer|diab[èe]te|d[ée]pression|anxi[ée]t[ée]"
              r"|burn-?out|enceinte|grossesse|th[ée]rapie|psychiatr\w*|psychologue|arr[êe]t maladie)\b"),
    ("opinions politiques ou religieuses", r"\b(?:religion|religieu\w*|croyant\w*|ath[ée]e|musulman\w*|catholique\w*"
              r"|chr[ée]tien\w*|juif|juive|juifs|bouddhiste\w*|parti politique|militant politique|[ée]lecteur de"
              r"|vote (?:à gauche|à droite|pour le parti)|[ée]lections? (?:présidentielles?|législatives|municipales"
              r"|européennes)|syndiqu[ée]\w*|opinions? politiques?)\b"),
    ("origine", r"\b(?:origine ethnique|ethnie|couleur de peau|racial\w*)\b"),
    ("vie intime", r"\b(?:orientation sexuelle|homosexu\w*|h[ée]t[ée]rosexu\w*|bisexu\w*|vie sexuelle|sexualit[ée])\b"),
]
_SENSIBLE = [(nom, re.compile(motif, re.IGNORECASE)) for nom, motif in _SENSIBLE]
_CARTE = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def _luhn(chiffres):
    total = 0
    for i, c in enumerate(reversed(chiffres)):
        n = int(c) * (2 if i % 2 else 1)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


def sensitive_reason(text):
    """Renvoie la catégorie sensible détectée dans le texte (« santé », « identifiants »…), ou None."""
    text = str(text or "")
    for nom, motif in _SENSIBLE:
        if motif.search(text):
            return nom
    for m in _CARTE.finditer(text):
        chiffres = re.sub(r"\D", "", m.group())
        if 13 <= len(chiffres) <= 19 and _luhn(chiffres):
            return "données bancaires"
    return None


def _check(content):
    reason = sensitive_reason(content)
    if reason:
        raise SensitiveError(f"Souvenir refusé : la mémoire ne retient jamais de {reason}.")


# ---------- journal des changements ----------
def _fields(m):
    keep = ("id", "category", "content", "source", "importance", "created_at", "updated_at", "event_ids",
            "confirmed_at", "valid_until")
    return {k: m.get(k) for k in keep} if m else None


def _log(op, memory_id, source="", event_ids="", before=None, after=None, reason=""):
    db.run("INSERT INTO memory_log(memory_id, op, source, event_ids, before_json, after_json, reason, created_at) "
           "VALUES(?,?,?,?,?,?,?,?)",
           (memory_id, op, str(source or "")[:120], str(event_ids or "")[:400],
            json.dumps(_fields(before), ensure_ascii=False) if before else None,
            json.dumps(_fields(after), ensure_ascii=False) if after else None, str(reason or "")[:300], db.now()))


def _get(mid):
    rows = db.run("SELECT * FROM memories WHERE id = ?", (mid,))
    return rows[0] if rows else None


def _today():
    return datetime.date.today().isoformat()


def _valid_until(value):
    value = str(value or "").strip()[:10]
    try:
        return datetime.date.fromisoformat(value).isoformat() if value else None
    except ValueError:
        return None


def recall(categories=None, limit=60, include_expired=False, espace=None):
    cats = [c for c in (categories or categories_de(espace)) if c in CATEGORIES]
    if not cats:
        return []
    marks = ", ".join("?" * len(cats))
    expiry = "" if include_expired else " AND (valid_until IS NULL OR valid_until = '' OR valid_until >= ?)"
    params = (*cats, *(() if include_expired else (_today(),)), int(limit))
    return db.run(f"SELECT * FROM memories WHERE category IN ({marks}){expiry} "
                  "ORDER BY importance DESC, updated_at DESC LIMIT ?", params)


def as_text(categories=None, limit=60, espace=None):
    """Bloc de texte à glisser dans le prompt d'un agent."""
    return format_items(recall(categories, limit, espace=espace))


def format_items(items):
    """Souvenirs en texte, groupés par catégorie dans l'ordre de CATEGORIES."""
    lines, current = [], None
    for m in sorted(items, key=lambda m: list(CATEGORIES).index(m["category"])):
        if m["category"] != current:
            current = m["category"]
            lines.append(f"{CATEGORIES[current]} :")
        lines.append(f"- {m['content']}")
    return "\n".join(lines)


def add(category, content, source="manuel", importance=3, event_ids="", valid_until=None):
    content = " ".join(str(content or "").split())[:600]
    if category not in CATEGORIES or not content:
        raise ValueError("Souvenir invalide.")
    _check(content)
    importance = max(1, min(5, int(importance or 3)))
    mid = db.run("INSERT INTO memories(category, content, source, importance, created_at, updated_at, event_ids, "
                 "confirmed_at, valid_until) VALUES(?,?,?,?,?,?,?,?,?) RETURNING id",
                 (category, content, source, importance, db.now(), db.now(), event_ids, db.now(),
                  _valid_until(valid_until)))[0]["id"]
    _log("add", mid, source, event_ids, after=_get(mid))
    return mid


def update(mid, content=None, category=None, importance=None, source="manuel", event_ids="", valid_until=None):
    m = _get(mid)
    if not m:
        return False
    content = " ".join(str(content if content is not None else m["content"]).split())[:600]
    _check(content)
    category = category if category in CATEGORIES else m["category"]
    importance = max(1, min(5, int(importance or m["importance"])))
    ids = ",".join(x for x in dict.fromkeys(str(m.get("event_ids") or "").split(",") + str(event_ids).split(",")) if x)
    until = _valid_until(valid_until) if valid_until is not None else m.get("valid_until")
    db.run("UPDATE memories SET content=?, category=?, importance=?, updated_at=?, event_ids=?, confirmed_at=?, "
           "valid_until=? WHERE id=?", (content, category, importance, db.now(), ids, db.now(), until, mid))
    _log("update", mid, source, event_ids, before=m, after=_get(mid))
    return True


def delete(mid, source="manuel", event_ids=""):
    m = _get(mid)
    if not m:
        return False
    db.run("DELETE FROM memories WHERE id = ?", (mid,))
    _log("delete", mid, source, event_ids, before=m)
    return True


def journal(limit=100):
    """Derniers changements, du plus récent au plus ancien. Efface d'abord les contenus trop anciens."""
    purge_journal()
    rows = db.run("SELECT * FROM memory_log ORDER BY id DESC LIMIT ?", (int(limit),))
    for r in rows:
        r["before"] = json.loads(r.pop("before_json") or "null")
        r["after"] = json.loads(r.pop("after_json") or "null")
        r["undoable"] = r["op"] in ("add", "update", "delete") and not r["undone_at"] and (r["before"] or r["after"])
    return rows


def purge_journal():
    """Au-delà de JOURNAL_DAYS jours, le journal garde la trace du changement mais plus son contenu."""
    limite = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=JOURNAL_DAYS)).isoformat(
        timespec="seconds")
    db.run("UPDATE memory_log SET before_json = NULL, after_json = NULL WHERE created_at < ? "
           "AND (before_json IS NOT NULL OR after_json IS NOT NULL)", (limite,))


def undo(log_id):
    """Annule un changement du journal : supprime un ajout, rétablit l'ancienne version d'une modification,
    recrée un souvenir supprimé. Une seule fois par changement."""
    rows = db.run("SELECT * FROM memory_log WHERE id = ?", (int(log_id),))
    if not rows:
        raise ValueError("Changement introuvable dans le journal.")
    entry = rows[0]
    if entry["undone_at"]:
        raise ValueError("Ce changement a déjà été annulé.")
    before, after = json.loads(entry["before_json"] or "null"), json.loads(entry["after_json"] or "null")
    mid, op = entry["memory_id"], entry["op"]
    current = _get(mid) if mid is not None else None
    if op == "add":
        if not after:
            raise ValueError("Changement trop ancien : son contenu n'est plus conservé.")
        if current:
            db.run("DELETE FROM memories WHERE id = ?", (mid,))
        _log("annulation", mid, "manuel", reason=f"annule l'ajout (journal n° {entry['id']})", before=current)
    elif op == "update":
        if not before:
            raise ValueError("Changement trop ancien : son contenu n'est plus conservé.")
        if not current:
            raise ValueError("Ce souvenir a été supprimé depuis : annule d'abord sa suppression.")
        db.run("UPDATE memories SET content=?, category=?, importance=?, updated_at=?, event_ids=?, confirmed_at=?, "
               "valid_until=? WHERE id=?", (before["content"], before["category"], before["importance"], db.now(),
                                            before.get("event_ids") or "", before.get("confirmed_at"),
                                            before.get("valid_until"), mid))
        _log("annulation", mid, "manuel", reason=f"rétablit la version précédente (journal n° {entry['id']})",
             before=current, after=_get(mid))
    elif op == "delete":
        if not before:
            raise ValueError("Changement trop ancien : son contenu n'est plus conservé.")
        if current:
            raise ValueError("Ce souvenir existe déjà.")
        db.run("INSERT INTO memories(id, category, content, source, importance, created_at, updated_at, event_ids, "
               "confirmed_at, valid_until) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (mid, before["category"], before["content"], before.get("source") or "", before["importance"],
                before.get("created_at"), db.now(), before.get("event_ids") or "", before.get("confirmed_at"),
                before.get("valid_until")))
        _log("annulation", mid, "manuel", reason=f"recrée le souvenir supprimé (journal n° {entry['id']})",
             after=_get(mid))
    else:
        raise ValueError("Ce changement ne peut pas être annulé.")
    db.run("UPDATE memory_log SET undone_at = ? WHERE id = ?", (db.now(), entry["id"]))
    return True


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

CATÉGORIES que tu peux utiliser pour ces événements (les autres appartiennent à d'autres agents) :
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
- Décisions techniques (comptes rendus de l'agent développeur) : retiens la règle ou le choix durable
  (« un dépôt par agent », « les secrets ne se règlent qu'à la main », un refus et sa raison), pas le
  détail d'une modification. Une modification ordinaire, fusionnée sans difficulté, ne mérite rien.
- Habitudes : seulement les siennes (rythme, horaires, façon de planifier), jamais ses contacts, ses
  rendez-vous avec d'autres personnes ni ses dépenses : ils restent chez l'agent perso.
- importance : 1 (anecdotique) à 5 (essentiel).
- Un fait temporaire (une période de disponibilité, un projet en cours) reçoit "valable_jusqu_au" :
  la date (AAAA-MM-JJ) après laquelle il ne sera plus utilisé. Aujourd'hui : {today}.

Format : liste JSON d'opérations, parmi
{{"op": "add", "category": "...", "content": "...", "importance": 3, "valable_jusqu_au": null}}
{{"op": "update", "id": 12, "content": "...", "importance": 4}}
{{"op": "delete", "id": 12}}"""


def process_pending():
    """Transforme les événements en attente en souvenirs. Renvoie le nombre d'opérations appliquées."""
    if not _processing.acquire(blocking=False):
        return 0
    applied = 0
    try:
        while True:
            # Un lot = les événements d'un seul agent, pour appliquer ses droits d'écriture
            first = db.run("SELECT agent FROM events WHERE processed = 0 ORDER BY id LIMIT 1")
            if not first:
                break
            agent = first[0]["agent"]
            events = db.run("SELECT * FROM events WHERE processed = 0 AND agent = ? ORDER BY id LIMIT 8", (agent,))
            allowed = categories_ecrites_par(agent)
            existing = recall(allowed, limit=MAX_MEMORIES_IN_PROMPT)
            prompt = PROMPT.format(
                today=_today(),
                categories="\n".join(f"- {k} : {CATEGORIES[k]}" for k in allowed),
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
            event_ids = ",".join(str(i) for i in ids)
            for op in ops if isinstance(ops, list) else []:
                try:
                    kind = op.get("op")
                    if kind == "add" and op.get("category") in CATEGORIES and op.get("category") not in allowed:
                        _log("refus", None, source, event_ids, reason=(
                            f"hors de son espace : l'agent « {agent} » ne peut pas écrire dans « {op.get('category')} »"))
                        continue
                    if kind == "add":
                        add(op.get("category"), op.get("content"), source, op.get("importance"), event_ids,
                            op.get("valable_jusqu_au"))
                    elif kind == "update" and int(op.get("id")) in known:
                        update(int(op["id"]), op.get("content"), None, op.get("importance"), source, event_ids,
                               op.get("valable_jusqu_au"))
                    elif kind == "delete" and int(op.get("id")) in known:
                        delete(int(op["id"]), source, event_ids)
                    else:
                        continue
                    applied += 1
                except SensitiveError as e:
                    # Le contenu refusé n'est pas conservé, même dans le journal : seule la raison l'est.
                    _log("refus", op.get("id") if kind != "add" else None, source, event_ids, reason=str(e))
                except (AttributeError, TypeError, ValueError):
                    continue
            db.run(f"UPDATE events SET processed = 1 WHERE id IN ({marks})", ids)
    finally:
        _processing.release()
    return applied
