"""Notebook backend: FastAPI + SQLite. Serves the API under /api and the frontend/ folder at /."""
import collections, io, ipaddress, json, math, os, re, socket, sqlite3
from contextlib import asynccontextmanager, closing
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()
DB = os.environ.get("DB_PATH", "notebook.db")
AI = bool(os.environ.get("OPENAI_API_KEY"))
FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
MAX_UPLOAD = 8 * 1024 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS notebooks(id INTEGER PRIMARY KEY, title TEXT NOT NULL DEFAULT 'Untitled notebook');
CREATE TABLE IF NOT EXISTS sources(id INTEGER PRIMARY KEY, notebook_id INTEGER NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
  name TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, notebook_id INTEGER NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
  role TEXT NOT NULL, content TEXT NOT NULL, citations TEXT NOT NULL DEFAULT '[]');
"""


@asynccontextmanager
async def lifespan(_):
    with closing(sqlite3.connect(DB)) as c:
        c.executescript(SCHEMA)
    yield


app = FastAPI(title="Notebook", lifespan=lifespan)


# ---------- db helpers ----------
def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def q(sql, args=()):
    with closing(conn()) as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def run(sql, args=()):
    with closing(conn()) as c:
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid


def need(nid):
    if not q("SELECT id FROM notebooks WHERE id=?", (nid,)):
        raise HTTPException(404, "Notebook not found")


# ---------- schemas ----------
class NotebookIn(BaseModel):
    title: str = "Untitled notebook"

class TextSourceIn(BaseModel):
    name: str = "Pasted text"
    text: str

class UrlIn(BaseModel):
    url: str

class EnabledIn(BaseModel):
    enabled: bool

class ChatIn(BaseModel):
    question: str


# ---------- retrieval ----------
STOP = set("the a an is are was were of to in on and or for with what how why who when which do does did it this that be as at by from about me my you your tell can i".split())
tok = lambda s: [w for w in re.findall(r"[a-z0-9']+", s.lower()) if w not in STOP and len(w) > 1]


def chunks(text, size=500):
    parts = [p.strip() for p in re.split(r"\n\s*\n|(?<=[.!?])\s+", text) if p.strip()]
    out, cur = [], ""
    for p in parts:
        if cur and len(cur) + len(p) > size:
            out.append(cur)
            cur = ""
        cur += (" " if cur else "") + p
    return out + ([cur] if cur else [])


def retrieve(srcs, question, k=5):
    """TF-IDF style ranking. Returns [(source_index, chunk)]."""
    if re.search(r"summar|overview|main points|tl;?dr", question, re.I):
        return [(i, chunks(s["text"])[0]) for i, s in enumerate(srcs) if s["text"].strip()][:k]
    docs = [(i, c) for i, s in enumerate(srcs) for c in chunks(s["text"])]
    toks = [tok(c) for _, c in docs]
    qt, df = set(tok(question)), collections.Counter()
    for t in toks:
        df.update(set(t))
    scored = []
    for (i, c), t in zip(docs, toks):
        tf = collections.Counter(t)
        sc = sum((1 + math.log(tf[w])) * math.log(1 + len(docs) / df[w]) for w in qt if w in tf)
        if sc > 0:
            scored.append((sc, i, c))
    scored.sort(key=lambda x: -x[0])
    return [(i, c) for _, i, c in scored[:k]]


def ask_openai(question, srcs, hits, history):
    from openai import OpenAI
    ctx = "\n\n".join(f"[{n}] ({srcs[i]['name']})\n{c}" for n, (i, c) in enumerate(hits, 1))
    r = OpenAI().chat.completions.create(
        model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        messages=[
            {"role": "system", "content": "Answer only from the numbered sources. Cite with [n]. If the sources don't contain the answer, say so."},
            *history,
            {"role": "user", "content": f"Sources:\n{ctx}\n\nQuestion: {question}"},
        ],
    )
    return r.choices[0].message.content


# ---------- web import ----------
def safe_url(u):
    p = urlparse(u)
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    try:
        ip = ipaddress.ip_address(socket.gethostbyname(p.hostname))
    except Exception:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local)


def fetch_page(url):
    if not safe_url(url):
        raise ValueError("That URL can't be fetched.")
    r = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0 notebook-app"})
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for t in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        t.decompose()
    title = (soup.title.string or url).strip() if soup.title else url
    text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n")).strip()
    if len(text) < 50:
        raise ValueError("No readable text found on that page.")
    return title[:120], text


def add_source(nid, name, kind, text):
    sid = run("INSERT INTO sources(notebook_id,name,kind,text) VALUES(?,?,?,?)", (nid, name, kind, text))
    return {"id": sid, "name": name, "kind": kind, "enabled": 1, "chars": len(text)}


# ---------- API ----------
@app.get("/api/health")
def health():
    return {"ai": AI}


@app.get("/api/notebooks")
def list_notebooks():
    return q("SELECT id,title FROM notebooks ORDER BY id DESC")


@app.post("/api/notebooks", status_code=201)
def create_notebook(body: NotebookIn):
    return {"id": run("INSERT INTO notebooks(title) VALUES(?)", (body.title[:120],)), "title": body.title}


@app.patch("/api/notebooks/{nid}")
def rename_notebook(nid: int, body: NotebookIn):
    need(nid)
    run("UPDATE notebooks SET title=? WHERE id=?", (body.title.strip()[:120] or "Untitled notebook", nid))
    return {"ok": True}


@app.delete("/api/notebooks/{nid}")
def delete_notebook(nid: int):
    run("DELETE FROM notebooks WHERE id=?", (nid,))
    return {"ok": True}


@app.get("/api/notebooks/{nid}/sources")
def list_sources(nid: int):
    need(nid)
    return q("SELECT id,name,kind,enabled,length(text) AS chars FROM sources WHERE notebook_id=? ORDER BY id", (nid,))


@app.post("/api/notebooks/{nid}/sources", status_code=201)
def paste_source(nid: int, body: TextSourceIn):
    need(nid)
    if not body.text.strip():
        raise HTTPException(400, "Paste some text first.")
    return add_source(nid, (body.name or "Pasted text")[:120], "text", body.text.strip())


@app.post("/api/notebooks/{nid}/sources/upload", status_code=201)
def upload_source(nid: int, file: UploadFile = File(...)):
    need(nid)
    name = file.filename or "file"
    data = file.file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "File is larger than 8 MB.")
    if name.lower().endswith(".pdf"):
        from pypdf import PdfReader
        text = "\n\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages)
    elif name.lower().endswith((".txt", ".md", ".csv")):
        text = data.decode("utf-8", errors="replace")
    else:
        raise HTTPException(400, "Supported files: .pdf .txt .md .csv")
    if not text.strip():
        raise HTTPException(400, "No readable text in that file.")
    return add_source(nid, name[:120], "file", text.strip())


@app.post("/api/notebooks/{nid}/sources/url", status_code=201)
def url_source(nid: int, body: UrlIn):
    need(nid)
    try:
        title, text = fetch_page(body.url.strip())
    except Exception as e:
        raise HTTPException(400, str(e))
    return add_source(nid, title, "web", text)


@app.patch("/api/sources/{sid}")
def toggle_source(sid: int, body: EnabledIn):
    run("UPDATE sources SET enabled=? WHERE id=?", (1 if body.enabled else 0, sid))
    return {"ok": True}


@app.delete("/api/sources/{sid}")
def delete_source(sid: int):
    run("DELETE FROM sources WHERE id=?", (sid,))
    return {"ok": True}


@app.get("/api/web-search")
def web_search(query: str = Query("", alias="q")):
    term = query.strip()
    if not term:
        return []
    try:
        r = requests.post("https://html.duckduckgo.com/html/", data={"q": term}, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
        out = []
        for a in BeautifulSoup(r.text, "html.parser").select("a.result__a")[:8]:
            href = a.get("href", "")
            if "uddg=" in href:
                href = parse_qs(urlparse(href).query).get("uddg", [href])[0]
            out.append({"title": a.get_text(strip=True), "url": href})
        return out
    except Exception:
        raise HTTPException(502, "Web search is unavailable right now.")


@app.get("/api/notebooks/{nid}/messages")
def messages(nid: int):
    need(nid)
    rows = q("SELECT role,content,citations FROM messages WHERE notebook_id=? ORDER BY id", (nid,))
    for r in rows:
        r["citations"] = json.loads(r["citations"])
    return rows


@app.post("/api/notebooks/{nid}/chat")
def chat(nid: int, body: ChatIn):
    need(nid)
    question = body.question.strip()
    if not question:
        raise HTTPException(400, "Type a question first.")
    srcs = q("SELECT id,name,text FROM sources WHERE notebook_id=? AND enabled=1", (nid,))
    if not srcs:
        raise HTTPException(400, "Add and select at least one source first.")
    hits = retrieve(srcs, question)
    if not hits:
        answer, cites = "I couldn't find that in your selected sources. Try other keywords or add a source that covers it.", []
    else:
        cites = [{"n": n, "source": srcs[i]["name"]} for n, (i, _) in enumerate(hits, 1)]
        if AI:
            rows = q("SELECT role,content FROM messages WHERE notebook_id=? ORDER BY id DESC LIMIT 6", (nid,))[::-1]
            try:
                answer = ask_openai(question, srcs, hits, [{"role": m["role"], "content": m["content"]} for m in rows])
            except Exception as e:
                raise HTTPException(502, f"AI request failed: {e}")
        else:
            answer = "\n\n".join(f"{c} [{n}]" for n, (_, c) in enumerate(hits, 1))
    run("INSERT INTO messages(notebook_id,role,content) VALUES(?,?,?)", (nid, "user", question))
    run("INSERT INTO messages(notebook_id,role,content,citations) VALUES(?,?,?,?)", (nid, "assistant", answer, json.dumps(cites)))
    return {"answer": answer, "citations": cites}


# Frontend (mounted last so /api routes win)
app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")
