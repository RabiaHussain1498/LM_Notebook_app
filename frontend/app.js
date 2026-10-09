// NotebookLM Client Application Engine
const $ = id => document.getElementById(id);
const esc = s => String(s || "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

let notebooks = [];
let currentNotebookId = null;
let sources = [];
let currentDocId = null;
let selectedDocText = "";

// API Client Helper
async function api(path, options = {}) {
  const opt = { method: options.method || "GET", headers: {} };
  if (options.json !== undefined) {
    opt.method = options.method || "POST";
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(options.json);
  }
  if (options.form) {
    opt.method = "POST";
    opt.body = options.form;
  }
  
  const res = await fetch("/api" + path, opt);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = data.detail || data.error || res.statusText;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return data;
}

// Toast Notifications
let toastTimeout;
function showToast(message) {
  const toast = $("toast");
  toast.textContent = message;
  toast.classList.add("active");
  clearTimeout(toastTimeout);
  toastTimeout = setTimeout(() => toast.classList.remove("active"), 3200);
}

const safeAction = fn => async (...args) => {
  try {
    await fn(...args);
  } catch (err) {
    console.error(err);
    showToast(err.message || "An error occurred.");
  }
};

// ----------------- Notebook Management -----------------

async function loadNotebooks(targetId) {
  notebooks = await api("/notebooks");
  if (!notebooks.length) {
    const created = await api("/notebooks", { json: { title: "Untitled notebook" } });
    notebooks = [created];
  }
  
  const savedId = targetId || +localStorage.getItem("active_nb") || notebooks[0].id;
  const match = notebooks.find(n => n.id === savedId) || notebooks[0];
  await selectNotebook(match.id);
}

async function updateNotebookTotalCost() {
  if (!currentNotebookId) return;
  try {
    const data = await api(`/notebooks/${currentNotebookId}/cost`);
    const cost = Number(data.total_cost || 0);
    const badgeVal = $("nb-total-cost");
    if (badgeVal) {
      badgeVal.textContent = `$${cost.toFixed(4)}`;
    }
  } catch (e) {
    console.warn("Cost update:", e);
  }
}

async function selectNotebook(id) {
  currentNotebookId = id;
  localStorage.setItem("active_nb", id);

  $("nb-select").innerHTML = notebooks.map(n => `<option value="${n.id}" ${n.id === id ? "selected" : ""}>${esc(n.title)}</option>`).join("");
  const activeNb = notebooks.find(n => n.id === id);
  const title = activeNb ? activeNb.title : "Untitled notebook";
  $("nb-title").value = title;
  $("canvas-heading").textContent = title;

  switchToSourcesListView();
  await refreshSources();
  await loadMessages();
  await updateNotebookTotalCost();
}

$("nb-select").onchange = safeAction(e => selectNotebook(+e.target.value));

$("btn-new-nb").onclick = safeAction(async () => {
  const nb = await api("/notebooks", { json: { title: "Untitled notebook" } });
  showToast("New notebook created.");
  await loadNotebooks(nb.id);
});

async function renameNotebook(newTitle) {
  const title = (newTitle || "").trim() || "Untitled notebook";
  await api(`/notebooks/${currentNotebookId}`, { method: "PATCH", json: { title } });
  $("nb-title").value = title;
  $("canvas-heading").textContent = title;
  notebooks = await api("/notebooks");
  $("nb-select").innerHTML = notebooks.map(n => `<option value="${n.id}" ${n.id === currentNotebookId ? "selected" : ""}>${esc(n.title)}</option>`).join("");
  showToast("Notebook renamed.");
}

$("nb-title").onchange = safeAction(async () => {
  await renameNotebook($("nb-title").value);
});

$("nb-title").onkeydown = e => {
  if (e.key === "Enter") {
    e.target.blur();
  }
};

const canvasHeading = $("canvas-heading");
canvasHeading.onblur = safeAction(async () => {
  await renameNotebook(canvasHeading.textContent);
});

canvasHeading.onkeydown = e => {
  if (e.key === "Enter") {
    e.preventDefault();
    canvasHeading.blur();
  }
};

$("btn-share").onclick = () => {
  navigator.clipboard?.writeText(location.href);
  showToast("Notebook link copied to clipboard.");
};

// ----------------- Left Sidebar: Sources & Preview Switching -----------------

function getKindIcon(kind) {
  switch (kind) {
    case "pdf": return "📕";
    case "code": return "💻";
    case "table": return "📊";
    case "web": return "🌐";
    default: return "📄";
  }
}

async function refreshSources() {
  sources = await api(`/notebooks/${currentNotebookId}/sources`);
  renderSourcesList();
}

function renderSourcesList() {
  const listEl = $("source-list");
  const count = sources.length;
  const countText = `${count} source${count === 1 ? "" : "s"}`;
  $("source-count-badge").textContent = countText;
  $("canvas-meta").textContent = `${countText} · ${new Date().toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })}`;

  if (!sources.length) {
    listEl.innerHTML = `
      <div style="padding: 24px 10px; text-align: center; color: var(--text-muted); font-size: 0.82rem;">
        Saved sources will appear here.<br>Add files, websites or notes, then explore with AI.
      </div>`;
    return;
  }

  listEl.innerHTML = sources.map(s => {
    const icon = getKindIcon(s.kind);
    const isSelected = currentDocId === s.id ? "selected-reading" : "";
    const chunks = s.chunks_count !== undefined ? s.chunks_count : 0;
    const chunkLabel = `${chunks} chunk${chunks === 1 ? "" : "s"}`;
    const cost = Number(s.embedding_cost || 0);
    const isOpenAI = s.embedding_model && s.embedding_model.startsWith("text-embedding");
    const costLabel = isOpenAI
      ? `<span class="source-cost-badge" title="OpenAI ${esc(s.embedding_model)} · embedding cost">⚡ $${cost.toFixed(6)}</span>`
      : `<span class="source-cost-badge source-cost-local" title="Indexed locally with TF-IDF — no API cost">local</span>`;
    return `
      <div class="source-item-row ${isSelected}" data-id="${s.id}">
        <span class="source-icon-badge">${icon}</span>
        <div class="source-row-info">
          <span class="source-row-title" title="${esc(s.name)}">${esc(s.name)}</span>
          <div class="source-row-meta">
            <span class="source-chunk-badge" title="${chunkLabel} indexed in ChromaDB">🧩 ${chunkLabel}</span>
            ${costLabel}
          </div>
        </div>
        <input type="checkbox" class="source-checkbox" data-id="${s.id}" ${s.enabled ? "checked" : ""} onclick="event.stopPropagation()">
      </div>
    `;
  }).join("");
}

// Switching views inside Left Sidebar
function switchToSourcesListView() {
  $("view-sources-list").classList.remove("hidden");
  $("view-doc-preview").classList.add("hidden");
  $("sidebar-title-text").textContent = "Sources";
  currentDocId = null;
  renderSourcesList();
}

async function openDocPreviewInLeftBar(sourceId, highlightText = null) {
  currentDocId = sourceId;
  const details = await api(`/sources/${sourceId}`);

  $("view-sources-list").classList.add("hidden");
  $("view-doc-preview").classList.remove("hidden");
  $("sidebar-title-text").textContent = "Sources";
  $("doc-preview-title").textContent = details.name;
  const chunksCount = details.chunks ? details.chunks.length : (details.chunks_count || 0);
  $("doc-emb-tag").textContent = `${details.embedding_model || "emb-3-small"} · ${chunksCount} chunk${chunksCount === 1 ? "" : "s"}`;

  const container = $("doc-content-body");
  // Always show the original full document text, not the internal RAG chunks
  const fullText = details.text || "";
  container.innerHTML = `<div class="doc-full-text" id="doc-full-text-${details.id}"></div>`;
  // Use textContent so no HTML injection; CSS pre-wrap preserves newlines
  $(`doc-full-text-${details.id}`).textContent = fullText;

  if (highlightText) {
    // Small delay lets the DOM settle before we try to highlight
    setTimeout(() => highlightInDocument(details.id, highlightText), 30);
  }
}

$("btn-back-to-sources").onclick = switchToSourcesListView;

// Search the full original document text and highlight the matching passage
function highlightInDocument(sourceId, highlightText) {
  const container = $(`doc-full-text-${sourceId}`);
  if (!container || !highlightText) return;

  const rawText = container.textContent;
  const collapseWS = s => s.replace(/[\u00a0\s]+/g, " ").trim();
  const normalizedRaw = collapseWS(rawText);

  // Strip any residual [Header] prefix old chunker stored at chunk start
  const cleanHL = highlightText.replace(/^\[[\s\S]*?\]\s*/, '').trim();

  // Multi-strategy search: most specific → least specific
  const strategies = [
    collapseWS(cleanHL),                                               // 1. full chunk
    cleanHL.length > 100 ? collapseWS(cleanHL.slice(0, 300)) : null,  // 2. first 300 chars
    cleanHL.length > 200                                               // 3. middle 200-char window
      ? collapseWS(cleanHL.slice(Math.floor(cleanHL.length * 0.2), Math.floor(cleanHL.length * 0.2) + 200))
      : null,
    (cleanHL.match(/^.{20,}?[.!?]/s) || [cleanHL.slice(0, 100)])[0]   // 4. first sentence
      ? collapseWS((cleanHL.match(/^.{20,}?[.!?]/s) || [cleanHL.slice(0, 100)])[0])
      : null,
  ].filter(Boolean);

  let normalizedHL = "";
  let normIdx = -1;
  for (const candidate of strategies) {
    normIdx = normalizedRaw.indexOf(candidate);
    if (normIdx >= 0) { normalizedHL = candidate; break; }
  }

  if (normIdx < 0) {
    container.scrollIntoView({ behavior: "smooth", block: "start" });
    container.classList.add("highlight-pulse");
    setTimeout(() => container.classList.remove("highlight-pulse"), 3000);
    return;
  }

  // Walk rawText char-by-char with parallel normalized cursor to map positions
  let rawStart = -1, rawEnd = -1;
  let ni = 0, ri = 0;
  const normEnd = normIdx + normalizedHL.length;

  while (ri < rawText.length) {
    if (/[\u00a0\s]/.test(rawText[ri])) {
      if (rawStart === -1 && ni === normIdx && normalizedRaw[ni] === " ") rawStart = ri;
      while (ri < rawText.length && /[\u00a0\s]/.test(rawText[ri])) ri++;
      if (ni === normEnd) { rawEnd = ri; break; }
      if (rawStart === -1 && ni + 1 === normIdx) rawStart = ri;
      ni++;
    } else {
      if (ni === normIdx && rawStart === -1) rawStart = ri;
      ri++; ni++;
      if (ni === normEnd) { rawEnd = ri; break; }
    }
  }

  if (rawStart >= 0 && rawEnd > rawStart) {
    const before = document.createTextNode(rawText.slice(0, rawStart));
    const mark = document.createElement("mark");
    mark.className = "citation-highlight";
    mark.textContent = rawText.slice(rawStart, rawEnd);
    const after = document.createTextNode(rawText.slice(rawEnd));
    container.replaceChildren(before, mark, after);
    mark.scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

// Left Source Click
$("source-list").onclick = safeAction(async e => {
  const row = e.target.closest(".source-item-row");
  if (row && !e.target.classList.contains("source-checkbox")) {
    const srcId = +row.dataset.id;
    await openDocPreviewInLeftBar(srcId);
  }
});

$("source-list").onchange = safeAction(async e => {
  if (e.target.classList.contains("source-checkbox")) {
    const id = +e.target.dataset.id;
    const enabled = e.target.checked;
    await api(`/sources/${id}`, { method: "PATCH", json: { enabled } });
    const s = sources.find(x => x.id === id);
    if (s) s.enabled = enabled ? 1 : 0;
  }
});

$("select-all-sources").onchange = safeAction(async e => {
  const checked = e.target.checked;
  for (const s of sources) {
    await api(`/sources/${s.id}`, { method: "PATCH", json: { enabled: checked } });
    s.enabled = checked ? 1 : 0;
  }
  renderSourcesList();
});

// Add Source Modal Dialog
const addDlg = $("dlg-add-source");
$("btn-add-source").onclick = () => addDlg.showModal();
$("dlg-close-btn").onclick = () => addDlg.close();
$("btn-cancel-source").onclick = () => addDlg.close();

$("btn-save-paste").onclick = safeAction(async () => {
  const text = $("paste-text").value.trim();
  const name = $("paste-title").value.trim() || "Pasted text";
  if (!text) return showToast("Paste some text first.");

  await api(`/notebooks/${currentNotebookId}/sources`, { json: { name, text, kind: "text" } });
  $("paste-text").value = "";
  $("paste-title").value = "";
  addDlg.close();
  await refreshSources();
  showToast("Source added.");
});

const handleUploadFiles = safeAction(async files => {
  addDlg.close();
  let totalChunks = 0;
  for (const f of files) {
    showToast(`Uploading ${f.name}...`);
    const fd = new FormData();
    fd.append("file", f);
    const res = await api(`/notebooks/${currentNotebookId}/sources/upload`, { form: fd });
    totalChunks += res.chunks_count || 0;
  }
  await refreshSources();
  await updateNotebookTotalCost();
  showToast(`Uploaded & indexed — ${totalChunks} chunk${totalChunks === 1 ? "" : "s"}`);
});

$("file-input").onchange = e => handleUploadFiles(e.target.files);

const dropZone = $("drop-zone");
["dragover", "dragenter"].forEach(ev => dropZone.addEventListener(ev, e => { e.preventDefault(); dropZone.classList.add("dragover"); }));
["dragleave", "drop"].forEach(ev => dropZone.addEventListener(ev, e => { e.preventDefault(); dropZone.classList.remove("dragover"); }));
dropZone.addEventListener("drop", e => handleUploadFiles(e.dataTransfer.files));

// ----------------- Text Highlighting Inside Left Preview -----------------

const highlightToolbar = $("highlight-toolbar");
const docBody = $("doc-content-body");

docBody.addEventListener("mouseup", () => {
  const selection = window.getSelection();
  const text = selection.toString().trim();
  if (text.length > 5) {
    selectedDocText = text;
    const range = selection.getRangeAt(0);
    const rect = range.getBoundingClientRect();
    highlightToolbar.style.top = `${rect.top - 40}px`;
    highlightToolbar.style.left = `${Math.max(10, rect.left + (rect.width / 2) - 100)}px`;
    highlightToolbar.style.display = "flex";
  } else {
    highlightToolbar.style.display = "none";
  }
});

document.addEventListener("mousedown", e => {
  if (!highlightToolbar.contains(e.target) && !docBody.contains(e.target)) {
    highlightToolbar.style.display = "none";
  }
});

function setContextPill(text) {
  $("context-text").textContent = text.length > 70 ? text.substring(0, 70) + "..." : text;
  $("context-pill").classList.remove("hidden");
}

$("btn-clear-context").onclick = () => {
  selectedDocText = "";
  $("context-pill").classList.add("hidden");
};

$("btn-ask-highlight").onclick = () => {
  setContextPill(selectedDocText);
  highlightToolbar.style.display = "none";
  $("chat-input").focus();
};

$("btn-summarize-highlight").onclick = safeAction(async () => {
  const passage = selectedDocText;
  highlightToolbar.style.display = "none";
  setContextPill(passage);
  $("chat-input").value = "Summarize the key points in this highlighted section.";
  await handleSendChat();
});

$("btn-explain-highlight").onclick = safeAction(async () => {
  const passage = selectedDocText;
  highlightToolbar.style.display = "none";
  setContextPill(passage);
  $("chat-input").value = "Explain this section in clear and accessible terms.";
  await handleSendChat();
});

// ----------------- Center Canvas: Chat & Interactive Citations -----------------

function scrollToBottom(smooth = true) {
  setTimeout(() => {
    const scrollEl = $("canvas-scroll");
    if (scrollEl) {
      scrollEl.scrollTo({
        top: scrollEl.scrollHeight,
        behavior: smooth ? "smooth" : "auto"
      });
    }
  }, 50);
}

async function loadMessages() {
  const chatEl = $("chat-messages");
  const messages = await api(`/notebooks/${currentNotebookId}/messages`);
  chatEl.innerHTML = "";
  messages.forEach(m => renderMessageBubble(m.role, m.content, m.citations, m));
  scrollToBottom(false);
}

function renderMessageBubble(role, content, citations = [], meta = {}) {
  const chatEl = $("chat-messages");
  const msgDiv = document.createElement("div");
  msgDiv.className = `chat-bubble ${role}`;

  const citationMap = new Map();
  for (const citation of citations) {
    citationMap.set(citation.n, citation);
  }

  const fragment = document.createDocumentFragment();
  const lines = content.split(/\n/);

  lines.forEach((line, lineIndex) => {
    if (lineIndex > 0) {
      fragment.appendChild(document.createElement("br"));
    }

    let offset = 0;
    const citationPattern = /\[(\d+)\]/g;
    let match;

    while ((match = citationPattern.exec(line))) {
      const textBefore = line.slice(offset, match.index);
      if (textBefore) {
        fragment.appendChild(document.createTextNode(textBefore));
      }

      const num = Number(match[1]);
      const citation = citationMap.get(num);
      const button = document.createElement("button");
      button.type = "button";
      button.className = "cite-pill";
      button.dataset.citeNum = String(num);
      button.dataset.sourceId = citation ? String(citation.source_id) : "";
      button.dataset.chunkIndex = citation ? String(citation.chunk_index) : "";
      button.dataset.citeContent = citation ? citation.content ?? citation.snippet : "";
      button.title = citation ? `View ${citation.source_name} [${num}]` : `Citation [${num}]`;
      button.textContent = `[${num}]`;
      button.disabled = !citation;
      fragment.appendChild(button);

      offset = match.index + match[0].length;
    }

    const trailingText = line.slice(offset);
    if (trailingText) {
      fragment.appendChild(document.createTextNode(trailingText));
    }
  });

  msgDiv.appendChild(fragment);

  // Render Query Time & Cost info on assistant messages
  if (role === "assistant" && meta && (meta.cost_usd !== undefined || meta.latency_s !== undefined)) {
    const infoLine = document.createElement("div");
    infoLine.className = "msg-query-meta";
    const costText = meta.cost_usd !== undefined ? `$${Number(meta.cost_usd).toFixed(6)}` : "$0.0000";
    const timeText = meta.latency_s !== undefined ? `${meta.latency_s}s` : "";
    const tokensText = meta.total_tokens ? `${meta.total_tokens} tokens` : (meta.prompt_tokens ? `${meta.prompt_tokens + (meta.completion_tokens || 0)} tokens` : "");

    const parts = [
      `<span>⚡ ${costText}</span>`,
      timeText ? `<span>⏱ ${timeText}</span>` : "",
      tokensText ? `<span>📊 ${tokensText}</span>` : ""
    ].filter(Boolean);

    infoLine.innerHTML = parts.join(' <span class="meta-sep">·</span> ');
    msgDiv.appendChild(infoLine);
  }

  chatEl.appendChild(msgDiv);
  scrollToBottom(true);
  return msgDiv;
}

// Click Citation in Chat -> Open Preview in Left Sidebar & Jump to passage
$("chat-messages").onclick = safeAction(async e => {
  const citeBtn = e.target.closest(".cite-pill");
  if (!citeBtn) return;

  const sourceId = Number(citeBtn.dataset.sourceId);
  const highlightText = citeBtn.dataset.citeContent || null;

  if (!Number.isInteger(sourceId) || sourceId <= 0) {
    throw new Error("Citation metadata is unavailable.");
  }

  await openDocPreviewInLeftBar(sourceId, highlightText);
  showToast(`Jumped to citation [${citeBtn.dataset.citeNum}]`);
});

// Chat Send Handler
async function handleSendChat() {
  const input = $("chat-input");
  const question = input.value.trim();
  if (!question) return;

  const context = selectedDocText;

  input.value = "";
  $("btn-clear-context").click();

  renderMessageBubble("user", question);
  const loadingBubble = renderMessageBubble("assistant", "Thinking...");

  try {
    const res = await api(`/notebooks/${currentNotebookId}/chat`, {
      json: {
        question,
        target_source_id: null,
        highlighted_context: context || null
      }
    });

    loadingBubble.remove();
    renderMessageBubble("assistant", res.answer, res.citations, res);
    await updateNotebookTotalCost();
  } catch (err) {
    loadingBubble.remove();
    showToast(err.message);
  }
}

$("btn-send-chat").onclick = safeAction(handleSendChat);
$("chat-input").onkeydown = e => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    handleSendChat();
  }
};

// ----------------- App Initialization -----------------

async function initApp() {
  await loadNotebooks();
}

window.addEventListener("DOMContentLoaded", () => {
  initApp().catch(err => console.error("Init error:", err));
});
