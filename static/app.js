"use strict";
const $ = (s) => document.querySelector(s);
async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await res.json().catch(() => ({ error: "Réponse illisible du serveur." }));
  if (res.status === 401) { location.href = "/login"; throw new Error("Connexion requise."); }
  if (!res.ok) throw new Error(data.error || "Erreur");
  return data;
}
function toast(msg, ms = 5000) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => (t.hidden = true), ms);
}
async function busy(btn, label, fn) {
  const old = btn.textContent; btn.disabled = true; btn.textContent = label;
  try { return await fn(); } catch (e) { toast(e.message, 8000); }
  finally { btn.disabled = false; btn.textContent = old; }
}
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function md(src) {
  const inline = (t) => esc(t).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  let html = "", list = false, para = [];
  const flush = () => { if (para.length) { html += `<p>${para.join("<br>")}</p>`; para = []; } };
  const endList = () => { if (list) { html += "</ul>"; list = false; } };
  for (const line of String(src || "").split("\n")) {
    const h = line.match(/^(#{1,3})\s+(.*)/), li = line.match(/^\s*[-*•]\s+(.*)/);
    if (h) { flush(); endList(); html += `<h3>${inline(h[2])}</h3>`; }
    else if (li) { flush(); if (!list) { html += '<ul class="plain">'; list = true; } html += `<li>${inline(li[1])}</li>`; }
    else if (!line.trim()) { flush(); endList(); }
    else { endList(); para.push(inline(line)); }
  }
  flush(); endList(); return html;
}
const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? u : "");
const AGENT_NAMES = { emploi: "via l'agent emploi", perso: "via l'agent perso", chef: "point du matin", dev: "agent développeur" };

// ---------- onglets ----------
const loaders = { chat: loadChat, board: loadBoard, memory: loadMemory, dev: loadDev };
document.querySelectorAll("nav button[data-tab]").forEach((b) => b.onclick = () => {
  document.querySelectorAll("nav button[data-tab]").forEach((x) => x.classList.toggle("active", x === b));
  for (const t of Object.keys(loaders)) $("#tab-" + t).hidden = t !== b.dataset.tab;
  loaders[b.dataset.tab]().catch((e) => toast(e.message));
});

// ---------- discussion ----------
function addMessage(m) {
  const div = document.createElement("div");
  div.className = "msg " + (m.role === "user" ? "user" : "bot");
  div.innerHTML = (m.agent && AGENT_NAMES[m.agent] ? `<div class="who">${AGENT_NAMES[m.agent]}</div>` : "")
    + `<div class="md">${md(m.content)}</div>`;
  $("#chat-log").appendChild(div); div.scrollIntoView({ block: "end" });
  return div;
}
async function loadChat() {
  const r = await api("/api/messages");
  $("#logout").hidden = !r.auth; $("#chat-log").innerHTML = "";
  if (!r.messages.length) addMessage({ role: "assistant", content: "Bonjour. Je coordonne tes agents et je retiens ce que j'apprends de toi. Que veux-tu faire ?" });
  r.messages.forEach(addMessage);
}
// Pièce jointe : un fichier choisi ou une photo prise avec l'appareil du téléphone
let attachment = null;   // { name, mime, data (base64) }
function readBase64(blob) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",")[1] || "");
    r.onerror = () => reject(new Error("Lecture du fichier impossible."));
    r.readAsDataURL(blob);
  });
}
// Une photo de téléphone pèse souvent plus de 5 Mo : on la réduit avant l'envoi (2000 px au plus)
async function shrinkImage(file) {
  try {
    const img = await createImageBitmap(file);
    const scale = Math.min(1, 2000 / Math.max(img.width, img.height));
    if (scale === 1 && file.size < 3e6) return file;
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(img.width * scale); canvas.height = Math.round(img.height * scale);
    canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise((ok) => canvas.toBlob(ok, "image/jpeg", 0.85));
    return blob ? new File([blob], file.name.replace(/\.[^.]*$/, "") + ".jpg", { type: "image/jpeg" }) : file;
  } catch { return file; }
}
async function pickFile(input) {
  let file = input.files[0]; input.value = "";
  if (!file) return;
  try {
    if (file.type.startsWith("image/")) file = await shrinkImage(file);
    if (file.size > 10e6) return toast("Fichier trop lourd (10 Mo au plus).");
    attachment = { name: file.name || "photo.jpg", mime: file.type || "application/octet-stream", data: await readBase64(file) };
    $("#chip-name").textContent = "📎 " + attachment.name; $("#chip").hidden = false;
    $("#chat-input").focus();
  } catch (er) { toast(er.message); }
}
$("#btn-file").onclick = () => $("#in-file").click();
$("#btn-photo").onclick = () => $("#in-photo").click();
$("#in-file").onchange = (e) => pickFile(e.target);
$("#in-photo").onchange = (e) => pickFile(e.target);
function clearAttachment() { attachment = null; $("#chip").hidden = true; }
$("#chip-remove").onclick = clearAttachment;

$("#chat-form").onsubmit = async (e) => {
  e.preventDefault();
  const text = $("#chat-input").value.trim(), file = attachment;
  if (!text && !file) return;
  $("#chat-input").value = ""; clearAttachment();
  addMessage({ role: "user", content: (text + (file ? `\n[pièce jointe : ${file.name}]` : "")).trim() });
  const wait = addMessage({ role: "assistant", content: "Je m'en occupe… (une recherche d'offres peut prendre plusieurs minutes)" });
  const btn = $("#chat-send"); btn.disabled = true;
  try {
    const r = await api("/api/chat", file ? { message: text, file } : { message: text });
    wait.remove(); addMessage({ role: "assistant", content: r.answer, agent: r.agent });
    if (r.remembered) toast("Noté dans ma mémoire.");
  } catch (er) { wait.remove(); addMessage({ role: "assistant", content: "Erreur : " + er.message }); }
  finally { btn.disabled = false; }
};
$("#chat-input").onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#chat-form").requestSubmit(); } };
$("#chat-clear").onclick = async () => {
  if (!confirm("Effacer la discussion ? La mémoire est conservée.")) return;
  await api("/api/chat/clear", {}); loadChat();
};

// ---------- tableau de bord ----------
function emploiCard(d) {
  const top = (d.top || []).slice(0, 5), rel = d.relances || [], st = d.by_status || {};
  return `<div class="stats">
      <div><b class="big">${d.total ?? 0}</b><span>offres suivies</span></div>
      <div><b class="big">${d.unscored ?? 0}</b><span>à noter</span></div>
      <div><b class="big">${st.postulee ?? 0}</b><span>postulées</span></div>
      <div><b class="big">${st.entretien ?? 0}</b><span>entretiens</span></div></div>
    ${top.length ? `<p><strong>Meilleures offres en cours</strong></p><ul class="plain">${top.map((o) =>
      `<li><strong>${esc(o.score)}</strong> — ${esc(o.title)} · ${esc(o.company || "?")} <small>(n° ${esc(o.id)})</small></li>`).join("")}</ul>` : ""}
    ${rel.length ? `<p><strong>À relancer</strong></p><ul class="plain">${rel.map((o) =>
      `<li>${esc(o.title)} · ${esc(o.company || "?")} <small>(postulée le ${esc(o.applied_at)})</small></li>`).join("")}</ul>` : ""}`;
}
async function loadBoard() {
  const d = await api("/api/dashboard");
  const b = $("#briefing");
  if (d.briefing) { b.hidden = false; b.innerHTML = `<h2>Point du matin — ${esc(d.briefing.date)}</h2><div class="md">${md(d.briefing.text)}</div>`; }
  else b.hidden = true;
  $("#board").innerHTML = d.agents.map((a) => {
    const url = safeUrl(a.url);
    const state = !a.configured ? '<span class="dot"></span>Non configuré'
      : a.ok ? '<span class="dot ok"></span>En ligne' : '<span class="dot ko"></span>Problème';
    return `<div class="card"><h2>${esc(a.name)}</h2><p class="hint">${esc(a.description)}</p>
      <p>${state}${a.error ? ` — ${esc(a.error)}` : ""}</p>
      ${a.ok && a.key === "emploi" ? emploiCard(a.data) : ""}
      ${url ? `<p><a href="${esc(url)}" target="_blank" rel="noopener noreferrer">Ouvrir l'agent</a></p>` : ""}</div>`;
  }).join("") + `<div class="card"><h2>Mémoire commune</h2><div class="stats">
      <div><b class="big">${d.memory_count}</b><span>souvenirs</span></div>
      <div><b class="big">${d.pending_events}</b><span>événements à traiter</span></div></div></div>`;
}
$("#board-refresh").onclick = (e) => busy(e.target, "Actualisation…", loadBoard);
$("#board-morning").onclick = (e) => busy(e.target, "Lancement…", async () => {
  await api("/api/morning", {});
  toast("Point du matin lancé en arrière-plan : actualise dans quelques minutes.", 8000);
});

// ---------- mémoire ----------
async function loadMemory() {
  const r = await api("/api/memories");
  const sel = $("#mem-cat");
  if (!sel.options.length) sel.innerHTML = Object.entries(r.categories).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  $("#mem-pending").innerHTML = r.pending_events
    ? `${r.pending_events} événement(s) en attente de traitement. <button id="mem-process">Traiter maintenant</button>` : "";
  const p = $("#mem-process");
  if (p) p.onclick = (e) => busy(e.target, "Traitement…", async () => { const x = await api("/api/memories/process", {}); toast(`${x.applied} souvenir(s) mis à jour.`); await loadMemory(); });
  $("#mem-list").innerHTML = Object.entries(r.categories).map(([k, label]) => {
    const items = r.items.filter((m) => m.category === k);
    return `<div class="card" style="margin-bottom:16px"><h2>${esc(label)} <small style="color:var(--mute)">(${items.length})</small></h2>
      ${items.map((m) => `<div class="mem" data-id="${m.id}"><div><span class="txt">${esc(m.content)}</span><br>
        <small>importance ${m.importance}/5 · source : ${esc(m.source || "?")} · ${esc((m.updated_at || "").slice(0, 10))}</small></div>
        <div><button data-act="edit">Modifier</button> <button data-act="del">Supprimer</button></div></div>`).join("")
        || '<p class="hint">Rien pour l\'instant.</p>'}</div>`;
  }).join("");
  $("#mem-list").querySelectorAll(".mem button").forEach((b) => b.onclick = async () => {
    const row = b.closest(".mem"), id = row.dataset.id;
    try {
      if (b.dataset.act === "del") { if (!confirm("Supprimer ce souvenir ?")) return; await api(`/api/memories/${id}/delete`, {}); }
      else {
        const text = prompt("Modifier le souvenir :", row.querySelector(".txt").textContent);
        if (text === null || !text.trim()) return;
        await api(`/api/memories/${id}/update`, { content: text });
      }
      await loadMemory();
    } catch (er) { toast(er.message); }
  });
}
$("#mem-add").onclick = (e) => busy(e.target, "Ajout…", async () => {
  const text = $("#mem-text").value.trim(); if (!text) return toast("Écris d'abord le souvenir.");
  await api("/api/memories", { category: $("#mem-cat").value, content: text, importance: 4 });
  $("#mem-text").value = ""; await loadMemory();
});

// ---------- agent développeur ----------
const DEV_STATUS = {
  en_cours: ["En cours", ""], fusionne: ["Fusionnée", "ok"], a_relire: ["À relire", "warn"],
  tests_rouges: ["Vérifications en échec", "ko"], ci_rouge: ["Tests du dépôt en échec", "ko"],
  sans_modification: ["Aucune modification", ""], refuse: ["Refusée", "ko"], erreur: ["Erreur", "ko"],
};
let devTimer = null;
async function loadDev() {
  const r = await api("/api/dev");
  const sel = $("#dev-depot");
  if (!sel.options.length) sel.innerHTML = Object.entries(r.depots).map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`).join("");
  const setup = $("#dev-setup");
  setup.hidden = r.configured && !r.note;
  setup.textContent = r.configured ? r.note : "Non configuré : ajoute la variable DEV_GITHUB_TOKEN dans Render (voir le README, « Agent développeur »).";
  $("#dev-send").disabled = !r.configured;
  const link = $("#dev-actions"); link.href = safeUrl(r.actions_url); link.hidden = !link.href;
  $("#dev-list").innerHTML = r.items.map((d) => {
    const [label, cls] = DEV_STATUS[d.statut] || [d.statut, ""], pr = safeUrl(d.pr_url), run = safeUrl(d.run_url);
    return `<div class="card" style="margin-bottom:12px">
      <p><span class="dot ${cls}"></span><strong>${esc(label)}</strong> · ${esc(r.depots[d.depot] || d.depot)}
        <small style="color:var(--mute)"> · ${esc((d.created_at || "").slice(0, 16).replace("T", " "))} UTC · réf. ${esc(d.ref)}</small></p>
      ${d.titre ? `<p><strong>${esc(d.titre)}</strong></p>` : ""}
      <p class="hint">Demande : ${esc(d.demande)}</p>
      ${d.resume ? `<details><summary>Compte rendu</summary><div class="md">${md(d.resume)}</div></details>` : ""}
      <p>${pr ? `<a href="${esc(pr)}" target="_blank" rel="noopener noreferrer">Pull request</a> ` : ""}
         ${run ? `<a href="${esc(run)}" target="_blank" rel="noopener noreferrer">Journal du travail</a>` : ""}</p></div>`;
  }).join("") || '<p class="hint">Aucune demande pour l\'instant.</p>';
  // Tant qu'une demande est en cours et que l'onglet est affiché, on actualise toutes les 30 secondes
  clearTimeout(devTimer);
  if (r.items.some((d) => d.statut === "en_cours"))
    devTimer = setTimeout(() => { if (!$("#tab-dev").hidden) loadDev().catch(() => {}); }, 30000);
}
$("#dev-refresh").onclick = (e) => busy(e.target, "Actualisation…", loadDev);
$("#dev-send").onclick = (e) => busy(e.target, "Lancement…", async () => {
  const text = $("#dev-text").value.trim();
  if (text.length < 10) return toast("Décris d'abord la modification voulue.");
  const name = $("#dev-depot").selectedOptions[0].textContent;
  if (!confirm(`Lancer cette demande sur « ${name} » ? Si les vérifications passent, la modification est fusionnée et déployée sans relecture.`)) return;
  await api("/api/dev", { depot: $("#dev-depot").value, demande: text });
  $("#dev-text").value = ""; toast("Demande lancée : le compte rendu arrivera ici et dans la discussion.", 8000);
  await loadDev();
});

loadChat().catch((e) => toast(e.message));
