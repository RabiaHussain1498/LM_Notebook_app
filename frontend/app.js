const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let nbs = [], cur = null, sources = [];

async function api(path, o = {}) {
  const opt = { method: o.method || "GET", headers: {} };
  if (o.json !== undefined) { opt.method = o.method || "POST"; opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(o.json); }
  if (o.form) { opt.method = "POST"; opt.body = o.form; }
  const r = await fetch("/api" + path, opt);
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { const m = d.error || d.detail; throw new Error(typeof m === "string" ? m : r.statusText); }
  return d;
}
let tt; const toast = m => { const t = $("toast"); t.textContent = m; t.classList.add("on"); clearTimeout(tt); tt = setTimeout(() => t.classList.remove("on"), 3000); };
const guard = fn => async (...a) => { try { await fn(...a); } catch (e) { toast(e.message); } };

// ----- notebooks -----
async function loadNotebooks(pick) {
  nbs = await api("/notebooks");
  if (!nbs.length) { await api("/notebooks", { json: {} }); nbs = await api("/notebooks"); }
  const id = pick || +localStorage.getItem("nb") || nbs[0].id;
  await openNotebook(nbs.find(n => n.id === id) ? id : nbs[0].id);
}
async function openNotebook(id) {
  cur = id; localStorage.setItem("nb", id);
  $("nbsel").innerHTML = nbs.map(n => `<option value="${n.id}" ${n.id === id ? "selected" : ""}>${esc(n.title)}</option>`).join("");
  $("title").value = nbs.find(n => n.id === id).title;
  sources = await api(`/notebooks/${id}/sources`); renderSources();
  const msgs = await api(`/notebooks/${id}/messages`);
  $("results").innerHTML = ""; $("chat").innerHTML = "";
  msgs.length ? msgs.forEach(m => bubble(m.role, m.content, m.citations)) : hero();
}
$("nbsel").onchange = guard(e => openNotebook(+e.target.value));
$("newnb").onclick = guard(async () => { const n = await api("/notebooks", { json: {} }); await loadNotebooks(n.id); });
$("delnb").onclick = guard(async () => { if (confirm("Delete this notebook and its sources?")) { await api(`/notebooks/${cur}`, { method: "DELETE" }); localStorage.removeItem("nb"); await loadNotebooks(); } });
$("title").onchange = guard(async () => { await api(`/notebooks/${cur}`, { method: "PATCH", json: { title: $("title").value } }); nbs = await api("/notebooks"); await openNotebook(cur); });
$("share").onclick = () => { navigator.clipboard?.writeText(location.href); toast("Link copied"); };
$("min").onclick = () => $("side").classList.toggle("min");

// ----- sources -----
function renderSources() {
  $("list").innerHTML = sources.length
    ? sources.map(s => `<div class="src"><input type="checkbox" data-id="${s.id}" ${s.enabled ? "checked" : ""} aria-label="Use ${esc(s.name)}"><span class="n" title="${esc(s.name)}">${esc(s.name)}</span><button class="x" data-del="${s.id}" aria-label="Remove ${esc(s.name)}">✕</button></div>`).join("")
    : `<div class="empty">Saved sources will appear here<br>Add files, websites or notes, then ask questions about them.</div>`;
  const n = sources.filter(s => s.enabled).length; $("cnt").textContent = n + (n === 1 ? " source" : " sources");
}
$("list").onchange = guard(async e => { const id = +e.target.dataset.id; await api(`/sources/${id}`, { method: "PATCH", json: { enabled: e.target.checked } }); sources.find(s => s.id === id).enabled = e.target.checked ? 1 : 0; renderSources(); });
$("list").onclick = guard(async e => { const id = e.target.dataset.del; if (!id) return; await api(`/sources/${id}`, { method: "DELETE" }); sources = sources.filter(s => s.id != id); renderSources(); });
const refreshSources = async () => { sources = await api(`/notebooks/${cur}/sources`); renderSources(); };

const dlg = $("dlg");
$("add").onclick = () => dlg.showModal();
$("cancel").onclick = () => dlg.close();
$("save").onclick = guard(async () => { await api(`/notebooks/${cur}/sources`, { json: { name: $("sname").value, text: $("stext").value } }); $("sname").value = $("stext").value = ""; dlg.close(); await refreshSources(); });
const upload = guard(async files => { dlg.close(); for (const f of files) { const fd = new FormData(); fd.append("file", f); await api(`/notebooks/${cur}/sources/upload`, { form: fd }); } await refreshSources(); toast("Source added"); });
$("file").onchange = e => upload(e.target.files);
const dr = $("drop");
["dragover", "dragenter"].forEach(t => dr.addEventListener(t, e => { e.preventDefault(); dr.classList.add("on"); }));
["dragleave", "drop"].forEach(t => dr.addEventListener(t, e => { e.preventDefault(); dr.classList.remove("on"); }));
dr.addEventListener("drop", e => upload(e.dataTransfer.files));

// web search / URL import
const importUrl = guard(async (url, btn) => { if (btn) btn.textContent = "Adding…"; try { await api(`/notebooks/${cur}/sources/url`, { json: { url } }); await refreshSources(); if (btn) btn.closest(".res").remove(); toast("Source added"); } catch (e) { if (btn) btn.textContent = "Add"; throw e; } });
$("webform").onsubmit = guard(async e => {
  e.preventDefault(); const v = $("webq").value.trim(); if (!v) return;
  if (/^https?:\/\//i.test(v)) { $("webq").value = ""; return importUrl(v); }
  $("results").innerHTML = '<div class="res">Searching…</div>';
  const rs = await api("/web-search?q=" + encodeURIComponent(v));
  $("results").innerHTML = rs.length ? rs.map(r => `<div class="res"><span title="${esc(r.url)}">${esc(r.title)}</span><button data-url="${esc(r.url)}">Add</button></div>`).join("") : '<div class="res">No results.</div>';
});
$("results").onclick = e => { if (e.target.dataset.url) importUrl(e.target.dataset.url, e.target); };

// ----- chat -----
function hero() {
  $("chat").innerHTML = `<div class="hero"><div style="font-size:48px">👋</div><h1>Let's start your notebook...</h1><p>This is your blank canvas to understand, create, or make progress on something new. Add sources, then ask questions about them.</p><h3>What would you like this notebook to help you do?</h3><button class="chip" data-a="learn">Learn about a new topic</button><button class="chip" data-a="create">Create something new</button><button class="chip" data-a="progress">Make progress on a project</button></div>`;
}
$("chat").onclick = e => {
  const a = e.target.dataset.a; if (!a) return;
  if (a === "learn") $("webq").focus(); else if (a === "create") dlg.showModal(); else { $("q").value = "What are the next steps?"; $("q").focus(); }
};
function bubble(role, text, cites = []) {
  const h = $("chat").querySelector(".hero"); if (h) h.remove();
  const d = document.createElement("div"); d.className = "msg " + role;
  d.innerHTML = esc(text).replace(/\[(\d+)\]/g, '<span class="cite">$1</span>');
  if (cites.length) d.innerHTML += `<div class="srcs">${cites.map(c => `${c.n}. ${esc(c.source)}`).join(" · ")}</div>`;
  $("chat").appendChild(d); $("chat").scrollTop = $("chat").scrollHeight; return d;
}
const ask = guard(async () => {
  const v = $("q").value.trim(); if (!v) return;
  $("q").value = ""; bubble("user", v); const wait = bubble("assistant", "Thinking…");
  try { const r = await api(`/notebooks/${cur}/chat`, { json: { question: v } }); wait.remove(); bubble("assistant", r.answer, r.citations); }
  catch (e) { wait.remove(); throw e; }
});
$("send").onclick = ask; $("q").onkeydown = e => { if (e.key === "Enter") ask(); };

guard(async () => { const h = await api("/health"); $("mode").textContent = h.ai ? "OpenAI" : "Local mode"; await loadNotebooks(); })();
