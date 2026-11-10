"""L'agent chef : point d'entrée unique (chat), tableau de bord et initiatives du matin."""
import datetime
import json
import threading
import time
from zoneinfo import ZoneInfo

import agents
import db
import llm
import memory

PARIS = ZoneInfo("Europe/Paris")
HISTORY = 10
_summary_cache = {"at": 0.0, "value": None}


def emploi_summary(max_age=120):
    """Résumé de l'agent emploi, gardé deux minutes pour ne pas le solliciter à chaque message."""
    if _summary_cache["value"] is not None and time.time() - _summary_cache["at"] < max_age:
        return _summary_cache["value"]
    value = agents.Emploi().summary()
    _summary_cache.update(at=time.time(), value=value)
    return value


def dashboard():
    cards = []
    for a in agents.all_agents():
        card = {"key": a.key, "name": a.name, "description": a.description, "url": a.url,
                "configured": a.configured, "ok": False, "error": "", "data": {}}
        if a.configured:
            try:
                card["data"] = emploi_summary(0) if a.key == "emploi" else a.summary()
                card["ok"] = True
            except agents.AgentError as e:
                card["error"] = str(e)
        cards.append(card)
    return {"agents": cards, "briefing": json.loads(db.kv_get("briefing") or "null"),
            "memory_count": db.run("SELECT COUNT(*) AS n FROM memories")[0]["n"],
            "pending_events": memory.pending_count()}


def _offers_text(offers):
    return "\n".join(f"- [{o['id']}] {o['title']} — {o.get('company') or '?'}, "
                     f"{o.get('location') or '?'} (score {o.get('score')})" for o in offers)


def _save(role, content, agent=""):
    db.run("INSERT INTO messages(role, content, agent, created_at) VALUES(?,?,?,?)",
           (role, content, agent, db.now()))


def history(limit=60):
    rows = db.run("SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,))
    return rows[::-1]


ROUTER = """Tu es l'agent chef de cette personne : son point d'entrée unique. Tu la connais grâce à ta
mémoire, tu réponds toi-même quand tu le peux, et tu confies le reste à l'agent spécialisé.

CE QUE TU SAIS D'ELLE (mémoire) :
{memory}

AGENTS DISPONIBLES :
{agents}

ÉTAT DE LA RECHERCHE D'EMPLOI :
{emploi}

CONVERSATION RÉCENTE :
{history}

NOUVEAU MESSAGE : {message}

Choisis UNE action et réponds en JSON :
{{"action": "...", "offer_id": null, "demande_perso": "", "reponse": "", "a_retenir": []}}

Actions :
- "repondre" : tu réponds toi-même dans "reponse" (conseil, question sur elle, sur ses offres déjà
  listées ci-dessus, discussion). Tutoie, sois direct et concret, appuie-toi sur la mémoire.
- "emploi_chercher" : lancer une nouvelle recherche d'offres et leur notation (long, plusieurs minutes).
- "emploi_candidature" : générer le CV et la lettre pour une offre ; mets son numéro dans "offer_id"
  (les numéros entre crochets ci-dessus). Si l'offre visée n'est pas claire, utilise "repondre" pour demander.
- "perso" : la demande concerne mails, agenda, tâches, dépenses ou planning ; reformule-la complètement
  dans "demande_perso" pour l'agent perso (il ne voit pas cette conversation).
N'utilise un agent que s'il est marqué disponible ; sinon explique dans "reponse" ce qu'il faut configurer.

"a_retenir" : liste (souvent vide) des informations durables que la personne vient de donner sur
elle-même, ses goûts, ses objectifs ou sa façon de travailler. Uniquement ce qu'elle a dit, mot pour
mot ou presque. Rien sur des tiers, rien de sensible (santé, opinions, finances)."""


def chat(message):
    message = str(message or "").strip()[:6000]
    if not message:
        raise ValueError("Message vide.")
    past = history(HISTORY)
    emploi, perso = agents.Emploi(), agents.Perso()
    state = "(agent emploi non disponible)"
    if emploi.configured:
        try:
            s = emploi_summary()
            state = (f"{s.get('total', 0)} offres suivies, {s.get('unscored', 0)} à noter, "
                     f"{len(s.get('relances', []))} relance(s) à faire.\nMeilleures offres en cours :\n"
                     + (_offers_text(s.get("top", [])) or "(aucune)"))
        except agents.AgentError as e:
            state = f"(agent emploi : {e})"
    plan = llm.ask_json(ROUTER.format(
        memory=memory.as_text() or "(rien pour l'instant)",
        agents="\n".join(f"- {a.key} ({a.name}) : {a.description} — "
                         f"{'disponible' if a.configured else 'NON CONFIGURÉ'}" for a in (emploi, perso)),
        emploi=state,
        history="\n".join(f"{'Elle' if m['role'] == 'user' else 'Toi'} : {m['content'][:700]}"
                          for m in past) or "(début de conversation)",
        message=message))
    if not isinstance(plan, dict):
        raise llm.LLMError("Réponse du modèle mal formée, réessaie.")
    action, agent_used = plan.get("action"), ""
    answer = str(plan.get("reponse") or "").strip()
    try:
        if action == "emploi_chercher":
            agent_used = "emploi"
            report, scored = emploi.search()
            _summary_cache["value"] = None
            new = sum(v.get("new", 0) for v in report.values() if isinstance(v, dict))
            errors = [f"{k} : {v['error']}" for k, v in report.items() if isinstance(v, dict) and v.get("error")]
            top = emploi_summary().get("top", [])[:6]
            answer = (f"Recherche terminée : {new} nouvelle(s) offre(s), {scored.get('scored', 0)} notée(s)"
                      + (f", {scored['left']} restent à noter" if scored.get("left") else "") + ".")
            if top:
                answer += "\n\nMeilleures offres en cours :\n" + _offers_text(top)
            if errors:
                answer += "\n\nSources en erreur :\n" + "\n".join("- " + e for e in errors)
        elif action == "emploi_candidature":
            agent_used = "emploi"
            docs = emploi.generate(int(plan.get("offer_id")))
            answer = ("CV et lettre générés, à relire et modifier dans l'agent emploi "
                      f"({emploi.url}).\n\nLettre :\n\n{docs.get('letter_md', '')}")
        elif action == "perso":
            agent_used = "perso"
            answer = perso.ask(str(plan.get("demande_perso") or message))
    except agents.AgentError as e:
        answer = f"Je n'ai pas pu joindre l'agent {agent_used} : {e}"
    except (TypeError, ValueError):
        answer = "Je n'ai pas compris de quelle offre tu parles : donne-moi son numéro ou son intitulé."
    answer = answer or "Je n'ai pas de réponse à cette demande."
    _save("user", message)
    _save("assistant", answer, agent_used)
    facts = [str(x).strip() for x in (plan.get("a_retenir") or []) if str(x).strip()][:5]
    if facts:
        memory.add_event("chef", "conversation", "La personne a dit :\n" + "\n".join("- " + f for f in facts))
    return {"answer": answer, "agent": agent_used, "remembered": len(facts)}


# ---------- initiatives ----------
def _morning():
    lines, today = [], datetime.datetime.now(PARIS).date().isoformat()
    emploi = agents.Emploi()
    if emploi.configured:
        try:
            before = {o["id"] for o in emploi.summary().get("top", [])}
            report, scored = emploi.search()
            _summary_cache["value"] = None
            s = emploi_summary(0)
            new = sum(v.get("new", 0) for v in report.values() if isinstance(v, dict))
            fresh = [o for o in s.get("top", []) if o["id"] not in before][:5]
            lines.append(f"Emploi : {new} nouvelle(s) offre(s) trouvée(s), {scored.get('scored', 0)} notée(s).")
            if fresh:
                lines.append("Nouvelles offres intéressantes :\n" + _offers_text(fresh))
            if s.get("relances"):
                lines.append("À relancer (candidature envoyée il y a 7 jours ou plus) :\n" + "\n".join(
                    f"- {o['title']} — {o.get('company') or '?'} (postulée le {o.get('applied_at')})"
                    for o in s["relances"]))
            for k, v in report.items():
                # Une source simplement non configurée n'est pas une panne : on ne la signale pas
                if isinstance(v, dict) and v.get("error") and "manquant" not in v["error"]:
                    lines.append(f"Source {k} en erreur : {v['error']}")
        except agents.AgentError as e:
            lines.append(f"Emploi : agent {e}")
    else:
        lines.append("Emploi : agent non configuré.")
    learned = memory.process_pending()
    if learned:
        lines.append(f"Mémoire : {learned} souvenir(s) mis à jour.")
    text = "\n\n".join(lines)
    db.kv_set("briefing", json.dumps({"date": today, "text": text}, ensure_ascii=False))
    _save("assistant", f"Point du matin ({today})\n\n{text}", "chef")


def start_morning(force=False):
    """Lance le point du matin en arrière-plan, une seule fois par jour (heure de Paris)."""
    today = datetime.datetime.now(PARIS).date().isoformat()
    if not force and db.kv_get("morning_date") == today:
        return {"status": "deja_fait"}
    db.kv_set("morning_date", today)
    threading.Thread(target=_morning, daemon=True).start()
    return {"status": "lance"}
