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
  const token = localStorage.getItem("nb_token");
  if (token) {
    opt.headers["Authorization"] = `Bearer ${token}`;
  }
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
}

$("nb-select").onchange = safeAction(e => selectNotebook(+e.target.value));

$("btn-new-nb").onclick = safeAction(async () => {
  const nb = await api("/notebooks", { json: { title: "Untitled notebook" } });
  showToast("New notebook created.");
  await loadNotebooks(nb.id);
});

$("nb-title").onchange = safeAction(async () => {
  const title = $("nb-title").value.trim() || "Untitled notebook";
  await api(`/notebooks/${currentNotebookId}`, { method: "PATCH", json: { title } });
  $("canvas-heading").textContent = title;
  notebooks = await api("/notebooks");
  showToast("Title updated.");
});

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
    return `
      <div class="source-item-row ${isSelected}" data-id="${s.id}">
        <span class="source-icon-badge">${icon}</span>
        <span class="source-row-title" title="${esc(s.name)}">${esc(s.name)}</span>
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

async function openDocPreviewInLeftBar(sourceId, highlightChunkIndex = null) {
  currentDocId = sourceId;
  const details = await api(`/sources/${sourceId}`);

  $("view-sources-list").classList.add("hidden");
  $("view-doc-preview").classList.remove("hidden");
  $("sidebar-title-text").textContent = "Sources";
  $("doc-preview-title").textContent = details.name;
  $("doc-emb-tag").textContent = details.embedding_model || "emb-3-small";

  const container = $("doc-content-body");
  if (details.chunks && details.chunks.length) {
    container.innerHTML = details.chunks.map(c => `
      <div class="chunk-row" id="chunk-${details.id}-${c.chunk_index}">
        <div class="chunk-text">${esc(c.content)}</div>
      </div>
    `).join("");
  } else {
    container.innerHTML = `<div class="chunk-row"><div class="chunk-text">${esc(details.text)}</div></div>`;
  }

  if (highlightChunkIndex !== null) {
    jumpAndHighlightChunkInPreview(details.id, highlightChunkIndex);
  }
}

$("btn-back-to-sources").onclick = switchToSourcesListView;

function jumpAndHighlightChunkInPreview(sourceId, chunkIndex) {
  const el = $(`chunk-${sourceId}-${chunkIndex}`);
  if (el) {
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    el.classList.add("highlight-pulse");
    setTimeout(() => el.classList.remove("highlight-pulse"), 3500);
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
  for (const f of files) {
    showToast(`Uploading ${f.name}...`);
    const fd = new FormData();
    fd.append("file", f);
    await api(`/notebooks/${currentNotebookId}/sources/upload`, { form: fd });
  }
  await refreshSources();
  showToast("Uploaded and indexed.");
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

async function loadMessages() {
  const chatEl = $("chat-messages");
  const messages = await api(`/notebooks/${currentNotebookId}/messages`);
  chatEl.innerHTML = "";
  messages.forEach(m => renderMessageBubble(m.role, m.content, m.citations, m));
}

function renderMessageBubble(role, content, citations = [], meta = {}) {
  const chatEl = $("chat-messages");
  const msgDiv = document.createElement("div");
  msgDiv.className = `chat-bubble ${role}`;

  let formattedContent = esc(content);

  // Markdown bold formatting (**text**)
  formattedContent = formattedContent.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');

  // Replace inline citations like [1], [2] with small inline pill buttons
  formattedContent = formattedContent.replace(/\[(\d+)\]/g, (match, num) => {
    return `<button class="cite-pill" data-cite-num="${num}" title="View source [${num}]">[${num}]</button>`;
  });

  // Preserve line breaks
  formattedContent = formattedContent.replace(/\n\n/g, '<br><br>').replace(/\n/g, '<br>');

  msgDiv.innerHTML = formattedContent;

  chatEl.appendChild(msgDiv);
  $("canvas-scroll").scrollTop = $("canvas-scroll").scrollHeight;
  return msgDiv;
}

// Click Citation in Chat -> Open Preview in Left Sidebar & Jump to Chunk
$("chat-messages").onclick = safeAction(async e => {
  const citeBtn = e.target.closest(".cite-pill");
  if (citeBtn) {
    const num = +citeBtn.dataset.citeNum;
    const msgs = await api(`/notebooks/${currentNotebookId}/messages`);
    const lastAssistantWithCites = msgs.reverse().find(m => m.role === "assistant" && m.citations && m.citations.length);
    if (lastAssistantWithCites) {
      const cite = lastAssistantWithCites.citations.find(c => c.n === num);
      if (cite) {
        await openDocPreviewInLeftBar(cite.source_id, cite.chunk_index);
        showToast(`Jumped to citation [${num}] in ${cite.source_name}`);
      }
    }
  }
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
