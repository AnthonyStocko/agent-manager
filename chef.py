"""L'agent chef : point d'entrée unique (chat), tableau de bord et initiatives du matin."""
import base64
import datetime
import json
import re
import threading
import time
from zoneinfo import ZoneInfo

import agents
import db
import dev
import files
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


def comments_text(data, limit=10):
    """Commentaires en attente de l'agent Instagram, en texte lisible dans la discussion."""
    items = data.get("items") or []
    if not items:
        return "Aucun commentaire en attente dans l'agent Instagram."
    lines = []
    for c in items[:limit]:
        head = " · ".join(x for x in (c.get("cible"), c.get("date")) if x)
        lines.append(f"- {head + ' : ' if head else ''}« {c.get('proposition') or '(texte vide)'} »"
                     + (f" ({c['post']})" if c.get("post") else ""))
    more = f"\n…et {len(items) - limit} autre(s)." if len(items) > limit else ""
    return (f"**{len(items)} commentaire(s) en attente** dans l'agent Instagram :\n" + "\n".join(lines) + more
            + "\n\nLa validation se fait dans l'agent Instagram.")


def _offers_text(offers):
    return "\n".join(f"- [{o['id']}] {o['title']} — {o.get('company') or '?'}, "
                     f"{o.get('location') or '?'} (score {o.get('score')})" for o in offers)


def _save(role, content, agent=""):
    db.run("INSERT INTO messages(role, content, agent, created_at) VALUES(?,?,?,?)",
           (role, content, agent, db.now()))


def history(limit=60):
    rows = db.run("SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,))
    return rows[::-1]


# ---------- agent perso : demandes, questions en attente et confirmations ----------
PENDING_MINUTES = 15
YES = re.compile(r"^\W*(oui|ok|okay|d'accord|dac|confirme[rz]?|je confirme|vas[- ]y|go|valide[rz]?|yes|c'est bon)\b", re.I)
NO = re.compile(r"^\W*(non|annule[rz]?|stop|laisse tomber|no)\b", re.I)


_held = {"file": None, "at": 0.0}   # pièce jointe en attente : en mémoire seulement, jamais sur le disque


def _hold(file):
    _held.update(file=file, at=time.time())


def _held_file():
    if _held["file"] and time.time() - _held["at"] < PENDING_MINUTES * 60:
        return _held["file"]
    _held["file"] = None
    return None


def _pending(key):
    """État en attente côté agent perso (question posée ou action à confirmer), s'il est encore valable."""
    try:
        state = json.loads(db.kv_get(key) or "null")
    except ValueError:
        state = None
    if state and time.time() - state.get("at", 0) < PENDING_MINUTES * 60:
        return state
    return None


def _set_pending(key, value):
    db.kv_set(key, json.dumps({**value, "at": time.time()}, ensure_ascii=False) if value else "null")


def _perso_result(result):
    """Résultat d'une fonctionnalité d'agent-perso, en texte lisible dans la discussion."""
    if not isinstance(result, dict):
        return str(result or "Fait.")
    if result.get("texte"):
        return str(result["texte"]) + (f"\n\n{result['note']}" if result.get("note") else "")
    if result.get("type") == "job":
        return "Traitement lancé : suis sa progression dans l'agent perso."
    raw = json.dumps({k: v for k, v in result.items() if k != "type"}, ensure_ascii=False, indent=1)
    return ("Résultat reçu (affichage détaillé dans l'agent perso) :\n" + raw[:1500]
            + ("\n…" if len(raw) > 1500 else ""))


def _perso_reply(res, text):
    """Met en forme la réponse de /assistant et retient ce qui attend une suite (question, confirmation)."""
    kind = res.get("type")
    if kind == "clarification":
        _set_pending("perso_question", {"text": text, "question": str(res.get("message") or "")})
        options = [str(x) for x in res.get("interpretations") or []]
        return str(res.get("message") or "Peux-tu préciser ?") + (
            "\n" + "\n".join("- " + o for o in options) if options else "")
    if kind in ("aide", "hors_perimetre"):
        feats = [f"- {f.get('titre')} : {f.get('description')}" for f in res.get("fonctionnalites") or []]
        return str(res.get("message") or "") + ("\n" + "\n".join(feats) if feats else "")
    parts, tokens, questions = [], [], []
    for a in res.get("actions") or []:
        title, status = a.get("titre") or a.get("fonction") or "Action", a.get("statut")
        if status == "fait":
            parts.append(f"**{title}**\n{_perso_result(a.get('resultat'))}")
        elif status == "a_confirmer":
            attach = a.get("fonction") == "creer_brouillon" and _held_file() is not None
            tokens.append({"jeton": a.get("jeton"), "resume": str(a.get("resume") or title), "attach": attach})
            parts.append(f"**{title}** — à confirmer :\n{a.get('resume') or ''}"
                         + (f"\nPièce jointe : {_held_file()['name']}" if attach else ""))
        elif status == "info_manquante":
            questions.append(str(a.get("question") or ""))
            parts.append(f"**{title}** — il me manque une information : {a.get('question') or ''}")
        elif status == "choix_agendas":
            parts.append(f"**{title}** — {a.get('question') or ''} Fais ce choix dans l'agent perso.")
        else:
            parts.append(f"**{title}** — erreur : {a.get('message') or 'inconnue'}")
    if tokens:
        _set_pending("perso_confirm", {"tokens": tokens})
        parts.append("Réponds **oui** pour confirmer, **non** pour annuler. Rien n'est enregistré avant.")
    if questions:
        _set_pending("perso_question", {"text": text, "question": " ".join(questions)})
    return "\n\n".join(parts) or "L'agent perso n'a rien renvoyé."


def _perso_confirm(state, confirmed):
    perso, lines = agents.Perso(), []
    for t in state.get("tokens", []):
        try:
            if confirmed and t.get("attach"):
                if not _held_file():
                    lines.append(f"Pièce jointe expirée, rien n'a été fait : renvoie ta demande avec le document ({t['resume']}).")
                    perso.confirm(t["jeton"], False)
                    continue
                r = perso.confirm_draft(t["jeton"], _held_file())
            else:
                r = perso.confirm(t["jeton"], confirmed)
            lines.append(f"Annulé : {t['resume']}" if r.get("statut") == "annule"
                         else f"**Fait.** {_perso_result(r.get('resultat'))}")
        except agents.AgentError as e:
            lines.append(f"Échec ({t['resume']}) : {e}")
    _set_pending("perso_confirm", None)
    _held["file"] = None
    return "\n\n".join(lines) or "Rien à confirmer."


def _expense_preview(file, sentence):
    """Photo d'un ticket : agent-perso lit la dépense, on demande confirmation avant d'écrire."""
    p = agents.Perso().receipt(file, sentence)
    _set_pending("perso_expense", {"preview": p})
    return (f"**Dépense lue sur le ticket** — à confirmer :\n- Montant : {p.get('amount_display') or p.get('amount')}\n"
            f"- Motif : {p.get('motif')}\n- Date : {p.get('date_display') or p.get('date')}\n"
            f"- Payée par : {p.get('person')}\n- Emplacement : {p.get('range')}\n\n"
            "Réponds **oui** pour l'enregistrer, **non** pour annuler. Si un champ est faux, réponds non "
            "et renvoie la photo avec la correction en une phrase.")


def _expense_confirm(state, confirmed):
    _set_pending("perso_expense", None)
    if not confirmed:
        return "Dépense annulée, rien n'a été enregistré."
    p = state["preview"]
    try:
        agents.Perso().confirm_expense(p)
    except agents.AgentError as e:
        return f"La dépense n'a pas été enregistrée : {e}"
    return f"**Dépense enregistrée** : {p.get('amount_display') or p.get('amount')} — {p.get('motif')} ({p.get('person')})."


ROUTER = """Tu es l'agent chef de cette personne : son point d'entrée unique. Tu la connais grâce à ta
mémoire, tu réponds toi-même quand tu le peux, et tu confies le reste à l'agent spécialisé.

CE QUE TU SAIS D'ELLE (mémoire) :
{memory}

AGENTS DISPONIBLES :
{agents}

ÉTAT DE LA RECHERCHE D'EMPLOI :
{emploi}

QUESTION EN ATTENTE DE L'AGENT PERSO : {question}

DÉPÔTS QUE L'AGENT DÉVELOPPEUR PEUT MODIFIER ({dev_state}) :
{depots}

PIÈCE JOINTE AU MESSAGE : {attachment}

CONVERSATION RÉCENTE :
{history}

NOUVEAU MESSAGE : {message}

Choisis UNE action et réponds en JSON :
{{"action": "...", "offer_id": null, "demande_perso": "", "depot": "", "demande_dev": "", "reponse": "", "a_retenir": []}}

Actions :
- "repondre" : tu réponds toi-même dans "reponse" (conseil, question sur elle, sur ses offres déjà
  listées ci-dessus, discussion). Tutoie, sois direct et concret, appuie-toi sur la mémoire.
- "emploi_chercher" : lancer une nouvelle recherche d'offres et leur notation (long, plusieurs minutes).
  Seulement si elle demande explicitement de chercher, relancer ou actualiser. Une question du type
  « y a-t-il des annonces à Lyon ? » porte sur les offres DÉJÀ suivies : réponds avec "repondre" à partir
  de la liste ci-dessus (filtre par lieu, dis honnêtement si la liste ne montre rien pour ce lieu, et
  propose de lancer une nouvelle recherche).
- "instagram_commentaires" : elle veut consulter les commentaires en attente (propositions de
  commentaires à valider) de l'agent Instagram. Lecture seule : tu ne valides ni ne publies rien.
- "emploi_candidature" : générer le CV et la lettre pour une offre ; mets son numéro dans "offer_id"
  (les numéros entre crochets ci-dessus). Si l'offre visée n'est pas claire, utilise "repondre" pour demander.
- "perso" : la demande concerne mails, agenda, tâches, dépenses, planning ou brouillons de mail ;
  recopie-la dans "demande_perso" pour l'agent perso (il ne voit pas cette conversation), en gardant
  tous les montants, noms et dates tels qu'ils sont écrits, sans rien ajouter. S'il y a une question
  en attente de l'agent perso et que le message y répond, choisis "perso" et mets dans
  "demande_perso" la réponse telle quelle. C'est lui qui enregistre les dépenses et crée les tâches :
  ne réponds jamais toi-même que c'est impossible.
- "perso_ticket" : la pièce jointe est la photo d'un ticket ou d'une facture à enregistrer en dépense ;
  mets dans "demande_perso" les précisions données (qui a payé, motif), sinon une chaîne vide.
- "emploi_cv" : la pièce jointe est un CV à ajouter au profil de recherche d'emploi.
- "emploi_annonce" : la pièce jointe est une offre d'emploi (document texte) à ajouter aux offres suivies.
- "dev" : elle demande de modifier, corriger ou améliorer le code d'un de ses agents ou de l'application
  Mon guide (nouvelle fonction, bug, comportement à changer). Mets dans "depot" la clé du dépôt visé
  (liste ci-dessus) et dans "demande_dev" la demande complète pour l'agent développeur, qui ne voit pas
  cette conversation : ce qui doit changer, le comportement attendu, les exemples donnés. Si le dépôt
  visé ou le changement voulu n'est pas clair, utilise "repondre" pour le demander. Une demande
  d'utiliser un agent (chercher des offres, traiter les mails) n'est pas une demande de développement.
Avec une pièce jointe et une demande de mail, choisis "perso" : le document sera joint au brouillon au
moment de la confirmation. Tu ne vois pas le contenu de la pièce jointe : ne le devine pas, et si son
usage n'est pas clair, demande-le avec "repondre" (elle reste disponible quelques minutes).
N'utilise un agent que s'il est marqué disponible ; sinon explique dans "reponse" ce qu'il faut configurer.

"a_retenir" : liste (souvent vide) des informations durables que la personne vient de donner sur
elle-même, ses goûts, ses objectifs ou sa façon de travailler. Uniquement ce qu'elle a dit, mot pour
mot ou presque. Rien sur des tiers, rien de sensible (santé, opinions, finances)."""


def _dev_confirm(state, confirmed):
    _set_pending("dev_confirm", None)
    if not confirmed:
        return "Demande annulée, rien n'a été lancé."
    try:
        ref = dev.submit(state["depot"], state["demande"])
    except dev.DevError as e:
        return f"La demande n'a pas pu être lancée : {e}"
    return (f"**Demande lancée** (référence {ref}). L'agent développeur travaille dans GitHub Actions, en "
            "général 5 à 20 minutes. Son compte rendu arrivera dans cette discussion ; l'avancement est "
            "dans l'onglet Développeur.")


def _direct(message, shown, answer, agent):
    _save("user", shown)
    _save("assistant", answer, agent)
    return {"answer": answer, "agent": agent, "remembered": 0}


def chat(message, file=None):
    message = str(message or "").strip()[:6000]
    file = files.decode(file)
    if not message and not file:
        raise ValueError("Message vide.")
    shown = (message + f"\n[pièce jointe : {file['name']}]").strip() if file else message
    # Réponse à une demande de confirmation de l'agent perso : traitée sans passer par le modèle
    expense, confirm, devel = _pending("perso_expense"), _pending("perso_confirm"), _pending("dev_confirm")
    if not file and (YES.match(message) or NO.match(message)):
        if devel:
            return _direct(message, shown, _dev_confirm(devel, bool(YES.match(message))), "dev")
        if expense:
            return _direct(message, shown, _expense_confirm(expense, bool(YES.match(message))), "perso")
        if confirm:
            return _direct(message, shown, _perso_confirm(confirm, bool(YES.match(message))), "perso")
    if expense:
        _set_pending("perso_expense", None)  # autre sujet : l'action en attente est abandonnée
    if confirm:
        _set_pending("perso_confirm", None)
    if devel:
        _set_pending("dev_confirm", None)
    if file:
        _hold(file)
        if not message:
            # Sans un mot : une photo est un ticket de dépense ; pour un document, on demande quoi en faire
            if files.is_image(file):
                try:
                    return _direct(message, shown, _expense_preview(file, ""), "perso")
                except agents.AgentError as e:
                    return _direct(message, shown, f"Je n'ai pas pu lire le ticket avec l'agent perso : {e}", "perso")
            return _direct(message, shown, (
                f"J'ai bien reçu « {file['name']} ». Que veux-tu en faire ?\n- l'ajouter à ton profil comme CV\n"
                "- l'ajouter à tes offres d'emploi suivies\n- le joindre à un mail (dis-moi à qui et pour quoi)"), "")
    file = _held_file()
    question = _pending("perso_question")
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
                         f"{'disponible' if a.configured else 'NON CONFIGURÉ'}" for a in (emploi, perso, agents.Instagram())),
        emploi=state,
        dev_state="disponible" if dev.configured() else "NON CONFIGURÉ : variable DEV_GITHUB_TOKEN manquante",
        depots="\n".join(f"- {k} : {v}" for k, v in dev.depots().items()),
        question=question["question"] if question else "(aucune)",
        attachment=(f"{file['name']} ({'image' if files.is_image(file) else 'document'})" if file else "(aucune)"),
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
            summary = emploi_summary()
            top = summary.get("top", [])[:6]
            answer = (f"Recherche terminée : {new} nouvelle(s) offre(s), {scored.get('scored', 0)} notée(s)"
                      + (f", {scored['left']} restent à noter" if scored.get("left") else "") + ".")
            if not new and summary.get("total"):
                answer += (f" Les {summary['total']} offres déjà suivies restent consultables dans l'agent emploi "
                           f"({emploi.url}) : « nouvelle » ne compte que ce que la recherche vient d'ajouter.")
            if top:
                answer += "\n\nMeilleures offres en cours :\n" + _offers_text(top)
            if errors:
                answer += "\n\nSources en erreur :\n" + "\n".join("- " + e for e in errors)
        elif action == "instagram_commentaires":
            agent_used = "instagram"
            answer = comments_text(agents.Instagram().pending_comments())
        elif action == "emploi_candidature":
            agent_used = "emploi"
            docs = emploi.generate(int(plan.get("offer_id")))
            answer = ("CV et lettre générés, à relire et modifier dans l'agent emploi "
                      f"({emploi.url}).\n\nLettre :\n\n{docs.get('letter_md', '')}")
        elif action in ("perso_ticket", "emploi_cv", "emploi_annonce") and not file:
            answer = "Je n'ai pas reçu de pièce jointe : renvoie-la avec ta demande."
        elif action == "perso_ticket":
            agent_used = "perso"
            if not files.is_image(file):
                answer = "Pour lire un ticket, il me faut une photo (image), pas un document."
            else:
                answer = _expense_preview(file, str(plan.get("demande_perso") or ""))
                _held["file"] = None
        elif action == "emploi_cv":
            agent_used = "emploi"
            r = emploi.import_cv(file["name"], base64.b64encode(file["data"]).decode())
            _held["file"] = None
            answer = (f"CV ajouté à ton profil dans l'agent emploi.\n\n{r.get('resume') or ''}\n\n"
                      f"Postes recherchés : {r.get('keywords') or '?'}")
            if r.get("a_verifier"):
                answer += "\n\nÀ vérifier dans tes CV :\n" + "\n".join("- " + x for x in r["a_verifier"])
        elif action == "emploi_annonce":
            agent_used = "emploi"
            if files.is_image(file):
                answer = ("Je ne sais pas encore lire une annonce en photo : envoie-la en PDF, "
                          "ou colle son texte ou son lien.")
            else:
                r = emploi.add_offer(files.extract_text(file["name"], file["data"]))
                _held["file"] = None
                _summary_cache["value"] = None
                answer = ("Offre ajoutée à ton suivi. Dis « note les nouvelles offres » ou ouvre l'agent emploi "
                          "pour la noter." if r.get("new") else "Cette offre est déjà dans ton suivi.")
        elif action == "dev":
            agent_used = "dev"
            depot, demande = str(plan.get("depot") or "").strip(), str(plan.get("demande_dev") or "").strip()
            if not dev.configured():
                answer = "L'agent développeur n'est pas configuré : ajoute la variable DEV_GITHUB_TOKEN (voir README)."
            elif depot not in dev.depots() or len(demande) < 10:
                answer = ("Quel agent faut-il modifier, et que doit-il faire différemment ? Dépôts possibles : "
                          + ", ".join(dev.depots().values()) + ".")
            else:
                _set_pending("dev_confirm", {"depot": depot, "demande": demande})
                answer = (f"**Demande pour l'agent développeur** — à confirmer :\n- Dépôt : {dev.depots()[depot]}\n"
                          f"- Demande : {demande}\n\nSi les vérifications passent, la modification est fusionnée "
                          "et déployée sans relecture. Réponds **oui** pour lancer, **non** pour annuler.")
        elif action == "perso":
            agent_used = "perso"
            text = str(plan.get("demande_perso") or message)
            _set_pending("perso_question", None)
            answer = _perso_reply(perso.assistant(text, question["text"] if question else ""), text)
    except agents.AgentError as e:
        answer = f"Je n'ai pas pu joindre l'agent {agent_used} : {e}"
    except files.ImportError_ as e:
        answer = str(e)
    except (TypeError, ValueError):
        answer = "Je n'ai pas compris de quelle offre tu parles : donne-moi son numéro ou son intitulé."
    answer = answer or "Je n'ai pas de réponse à cette demande."
    _save("user", shown)
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
