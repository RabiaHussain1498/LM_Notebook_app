"""
NotebookLM-Style AI Workspace: FastAPI + SQLite + Embeddings + Session Auth + Cost Tracking + In-Doc Citations & API Tools
"""
import collections
import hashlib
import hmac
import io
import ipaddress
import json
import math
import os
import re
import secrets
import socket
import sqlite3
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fastapi import Cookie, Depends, FastAPI, File, Header, HTTPException, Query, Response, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()
DB = os.environ.get("DB_PATH", "notebook.db")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
MAX_UPLOAD_BYTES = 16 * 1024 * 1024  # 16 MB

# Pricing constants (USD per token)
PRICING = {
    "gpt-4o-mini": {"input": 0.150 / 1_000_000, "output": 0.600 / 1_000_000},
    "gpt-4o": {"input": 2.500 / 1_000_000, "output": 10.000 / 1_000_000},
    "text-embedding-3-small": {"input": 0.020 / 1_000_000, "output": 0.0},
    "text-embedding-3-large": {"input": 0.130 / 1_000_000, "output": 0.0},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    total_cost REAL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS notebooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL DEFAULT 'Untitled notebook',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notebook_id INTEGER NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    embedding_model TEXT DEFAULT 'tf-idf-local',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    embedding TEXT,
    tokens INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notebook_id INTEGER NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    citations TEXT NOT NULL DEFAULT '[]',
    target_source_id INTEGER,
    cost_usd REAL DEFAULT 0.0,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS attached_apis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notebook_id INTEGER NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    method TEXT NOT NULL DEFAULT 'GET',
    headers TEXT NOT NULL DEFAULT '{}',
    description TEXT DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS usage_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    notebook_id INTEGER REFERENCES notebooks(id) ON DELETE SET NULL,
    operation TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0.0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def db_query(sql: str, args: tuple = ()):
    with closing(get_db()) as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]

def db_execute(sql: str, args: tuple = ()):
    with closing(get_db()) as c:
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid

def hash_password(password: str, salt: Optional[str] = None) -> Tuple[str, str]:
    if not salt:
        salt = secrets.token_hex(16)
    pw_hash = hashlib.sha256((password + salt).encode("utf-8")).hexdigest()
    return pw_hash, salt

def verify_password(password: str, pw_hash: str, salt: str) -> bool:
    expected_hash, _ = hash_password(password, salt)
    return hmac.compare_digest(expected_hash, pw_hash)

def migrate_db(c):
    cur = c.cursor()
    cur.execute("PRAGMA table_info(notebooks)")
    cols = [r[1] for r in cur.fetchall()]
    if "user_id" not in cols:
        cur.execute("ALTER TABLE notebooks ADD COLUMN user_id INTEGER DEFAULT 1")
    if "created_at" not in cols:
        cur.execute("ALTER TABLE notebooks ADD COLUMN created_at TIMESTAMP")

    cur.execute("PRAGMA table_info(sources)")
    cols = [r[1] for r in cur.fetchall()]
    if "embedding_model" not in cols:
        cur.execute("ALTER TABLE sources ADD COLUMN embedding_model TEXT DEFAULT 'tf-idf-local'")
    if "created_at" not in cols:
        cur.execute("ALTER TABLE sources ADD COLUMN created_at TIMESTAMP")

    cur.execute("PRAGMA table_info(messages)")
    cols = [r[1] for r in cur.fetchall()]
    if "user_id" not in cols:
        cur.execute("ALTER TABLE messages ADD COLUMN user_id INTEGER DEFAULT 1")
    if "target_source_id" not in cols:
        cur.execute("ALTER TABLE messages ADD COLUMN target_source_id INTEGER")
    if "cost_usd" not in cols:
        cur.execute("ALTER TABLE messages ADD COLUMN cost_usd REAL DEFAULT 0.0")
    if "prompt_tokens" not in cols:
        cur.execute("ALTER TABLE messages ADD COLUMN prompt_tokens INTEGER DEFAULT 0")
    if "completion_tokens" not in cols:
        cur.execute("ALTER TABLE messages ADD COLUMN completion_tokens INTEGER DEFAULT 0")
    if "created_at" not in cols:
        cur.execute("ALTER TABLE messages ADD COLUMN created_at TIMESTAMP")
    c.commit()

def ensure_default_user():
    user = db_query("SELECT id FROM users WHERE username = 'demo_user'")
    if not user:
        pw_hash, salt = hash_password("demo123")
        db_execute("INSERT INTO users(username, password_hash, salt) VALUES (?, ?, ?)", ("demo_user", pw_hash, salt))

@asynccontextmanager
async def lifespan(app: FastAPI):
    with closing(sqlite3.connect(DB)) as c:
        c.executescript(SCHEMA)
        migrate_db(c)
    ensure_default_user()
    yield

app = FastAPI(title="NotebookLM AI App", lifespan=lifespan)

async def get_current_user(
    authorization: Optional[str] = Header(None),
    session_token: Optional[str] = Cookie(None)
) -> dict:
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ", 1)[1].strip()
    elif session_token:
        token = session_token

    if token:
        sess = db_query("SELECT user_id, expires_at FROM sessions WHERE token = ?", (token,))
        if sess:
            expires = datetime.fromisoformat(sess[0]["expires_at"])
            if expires > datetime.utcnow():
                users = db_query("SELECT id, username, total_cost FROM users WHERE id = ?", (sess[0]["user_id"],))
                if users:
                    return users[0]

    demo = db_query("SELECT id, username, total_cost FROM users WHERE username = 'demo_user'")
    if demo:
        return demo[0]
    
    pw_hash, salt = hash_password("demo123")
    uid = db_execute("INSERT INTO users(username, password_hash, salt) VALUES (?, ?, ?)", ("demo_user", pw_hash, salt))
    return {"id": uid, "username": "demo_user", "total_cost": 0.0}

STOPWORDS = set("the a an is are was were of to in on and or for with what how why who when which do does did it this that be as at by from about me my you your tell can i we us they them he she had have has".split())

def tokenize(text: str) -> List[str]:
    return [w for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in STOPWORDS and len(w) > 1]

def chunk_text(text: str, kind: str = "text", size: int = 550, overlap: int = 60) -> List[str]:
    if kind in ("code", "table"):
        lines = [line for line in text.split("\n") if line.strip()]
        chunks_out, cur = [], ""
        for line in lines:
            if cur and len(cur) + len(line) > size:
                chunks_out.append(cur)
                cur = ""
            cur += ("\n" if cur else "") + line
        if cur:
            chunks_out.append(cur)
        return chunks_out or [text[:size]]

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n|(?<=[.!?])\s+", text) if p.strip()]
    chunks_out, cur = [], ""
    for p in paragraphs:
        if cur and len(cur) + len(p) > size:
            chunks_out.append(cur)
            cur = cur[-overlap:] if len(cur) > overlap else ""
        cur += (" " if cur else "") + p
    if cur:
        chunks_out.append(cur)
    return chunks_out or [text[:size]]

def select_embedding_model(kind: str, text: str) -> str:
    if not OPENAI_KEY:
        return "tf-idf-local"
    if kind in ("code", "table") or len(text) > 20000:
        return "text-embedding-3-large"
    return "text-embedding-3-small"

def compute_openai_embedding(texts: List[str], model: str) -> Tuple[List[List[float]], int, float]:
    if not OPENAI_KEY:
        return [], 0, 0.0
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_KEY)
    response = client.embeddings.create(input=texts, model=model)
    embeddings = [item.embedding for item in response.data]
    tokens = response.usage.total_tokens
    rate = PRICING.get(model, {}).get("input", 0.020 / 1_000_000)
    cost = tokens * rate
    return embeddings, tokens, cost

def cosine_similarity(v1: List[float], v2: List[float]) -> float:
    dot = sum(a * b for a, b in zip(v1, v2))
    norm1 = math.sqrt(sum(a * a for a in v1))
    norm2 = math.sqrt(sum(b * b for b in v2))
    if norm1 == 0 or norm2 == 0:
        return 0.0
    return dot / (norm1 * norm2)

def retrieve_top_chunks(
    notebook_id: int,
    query: str,
    target_source_id: Optional[int] = None,
    k: int = 5
) -> Tuple[List[Dict[str, Any]], str, float]:
    where_clauses = ["s.notebook_id = ?", "s.enabled = 1"]
    params: List[Any] = [notebook_id]
    if target_source_id:
        where_clauses.append("s.id = ?")
        params.append(target_source_id)

    sql = f"""
        SELECT c.id AS chunk_id, c.chunk_index, c.content, c.embedding, s.id AS source_id, s.name AS source_name, s.kind
        FROM chunks c
        JOIN sources s ON c.source_id = s.id
        WHERE {" AND ".join(where_clauses)}
    """
    raw_chunks = db_query(sql, tuple(params))
    if not raw_chunks:
        return [], "none", 0.0

    retrieval_cost = 0.0
    strategy_used = "tf-idf"

    has_vectors = raw_chunks and raw_chunks[0]["embedding"] is not None and OPENAI_KEY
    if has_vectors:
        try:
            emb_model = "text-embedding-3-small"
            q_embeddings, tokens, cost = compute_openai_embedding([query], emb_model)
            retrieval_cost += cost
            strategy_used = emb_model
            q_vec = q_embeddings[0]

            scored_chunks = []
            for item in raw_chunks:
                if item["embedding"]:
                    vec = json.loads(item["embedding"])
                    score = cosine_similarity(q_vec, vec)
                    scored_chunks.append((score, item))
            
            scored_chunks.sort(key=lambda x: -x[0])
            top_results = [item for score, item in scored_chunks[:k] if score > 0.15]
            if top_results:
                return top_results, strategy_used, retrieval_cost
        except Exception as e:
            print(f"Vector search fallback: {e}")

    strategy_used = "tf-idf-local"
    if re.search(r"summar|overview|main points|tl;?dr", query, re.I):
        top_chunks = []
        seen_sources = set()
        for c in raw_chunks:
            if c["source_id"] not in seen_sources:
                top_chunks.append(c)
                seen_sources.add(c["source_id"])
            if len(top_chunks) >= k:
                break
        return top_chunks, strategy_used, retrieval_cost

    docs = raw_chunks
    tokenized_docs = [tokenize(d["content"]) for d in docs]
    q_tokens = set(tokenize(query))
    df = collections.Counter()
    for t_list in tokenized_docs:
        df.update(set(t_list))

    scored = []
    num_docs = len(docs)
    for doc, tokens in zip(docs, tokenized_docs):
        tf = collections.Counter(tokens)
        score = sum((1 + math.log(tf[w])) * math.log(1 + num_docs / (df[w] or 1)) for w in q_tokens if w in tf)
        if score > 0:
            scored.append((score, doc))

    scored.sort(key=lambda x: -x[0])
    return [doc for _, doc in scored[:k]], strategy_used, retrieval_cost

def execute_attached_api(api_id: int, user_params: Dict[str, Any] = {}) -> Dict[str, Any]:
    apis = db_query("SELECT id, name, url, method, headers, description FROM attached_apis WHERE id = ? AND enabled = 1", (api_id,))
    if not apis:
        raise HTTPException(404, "Attached API not found or disabled.")
    api_info = apis[0]
    headers = json.loads(api_info["headers"] or "{}")
    url = api_info["url"]
    method = api_info["method"].upper()
    
    try:
        if method == "GET":
            resp = requests.get(url, params=user_params, headers=headers, timeout=10)
        else:
            resp = requests.post(url, json=user_params, headers=headers, timeout=10)
        return {
            "api_name": api_info["name"],
            "status": resp.status_code,
            "data": resp.json() if "application/json" in resp.headers.get("Content-Type", "") else resp.text[:2000]
        }
    except Exception as e:
        return {"api_name": api_info["name"], "error": str(e)}

def safe_url(u: str) -> bool:
    try:
        p = urlparse(u)
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        ip = ipaddress.ip_address(socket.gethostbyname(p.hostname))
        return not (ip.is_private or ip.is_loopback or ip.is_link_local)
    except Exception:
        return False

def fetch_web_page(url: str) -> Tuple[str, str]:
    if not safe_url(url):
        raise ValueError("URL is invalid or blocked for security reasons.")
    res = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0 NotebookLM-App/2.0"})
    res.raise_for_status()
    soup = BeautifulSoup(res.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "svg"]):
        tag.decompose()
    title = (soup.title.string or url).strip() if soup.title else url
    text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n")).strip()
    if len(text) < 40:
        raise ValueError("No readable text found on that page.")
    return title[:120], text

# Pydantic models
class AuthIn(BaseModel):
    username: str
    password: str

class NotebookIn(BaseModel):
    title: str = "Untitled notebook"

class TextSourceIn(BaseModel):
    name: str = "Pasted note"
    text: str
    kind: str = "text"

class UrlIn(BaseModel):
    url: str

class EnabledIn(BaseModel):
    enabled: bool

class ChatIn(BaseModel):
    question: str
    target_source_id: Optional[int] = None
    highlighted_context: Optional[str] = None

class AttachedApiIn(BaseModel):
    name: str
    url: str
    method: str = "GET"
    headers: str = "{}"
    description: str = ""

# API Endpoints
@app.post("/api/auth/register")
def register(body: AuthIn, response: Response):
    if len(body.username.strip()) < 3 or len(body.password) < 4:
        raise HTTPException(400, "Username and password must be at least 3-4 characters.")
    existing = db_query("SELECT id FROM users WHERE username = ?", (body.username.strip(),))
    if existing:
        raise HTTPException(400, "Username is already taken.")
    pw_hash, salt = hash_password(body.password)
    uid = db_execute("INSERT INTO users(username, password_hash, salt) VALUES (?, ?, ?)", (body.username.strip(), pw_hash, salt))
    
    token = secrets.token_hex(32)
    expires = datetime.utcnow() + timedelta(days=7)
    db_execute("INSERT INTO sessions(token, user_id, expires_at) VALUES (?, ?, ?)", (token, uid, expires.isoformat()))
    response.set_cookie("session_token", token, max_age=7*86400, httponly=True)
    return {"token": token, "username": body.username.strip(), "user_id": uid}

@app.post("/api/auth/login")
def login(body: AuthIn, response: Response):
    users = db_query("SELECT id, username, password_hash, salt, total_cost FROM users WHERE username = ?", (body.username.strip(),))
    if not users or not verify_password(body.password, users[0]["password_hash"], users[0]["salt"]):
        raise HTTPException(401, "Invalid username or password.")
    user = users[0]
    token = secrets.token_hex(32)
    expires = datetime.utcnow() + timedelta(days=7)
    db_execute("INSERT INTO sessions(token, user_id, expires_at) VALUES (?, ?, ?)", (token, user["id"], expires.isoformat()))
    response.set_cookie("session_token", token, max_age=7*86400, httponly=True)
    return {"token": token, "username": user["username"], "user_id": user["id"], "total_cost": user["total_cost"]}

@app.post("/api/auth/logout")
def logout(response: Response, user: dict = Depends(get_current_user)):
    db_execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
    response.delete_cookie("session_token")
    return {"ok": True}

@app.get("/api/auth/me")
def get_me(user: dict = Depends(get_current_user)):
    recent_costs = db_query("SELECT SUM(cost_usd) as total_spent, SUM(prompt_tokens) as total_p_tokens, SUM(completion_tokens) as total_c_tokens FROM usage_logs WHERE user_id = ?", (user["id"],))
    spent = (recent_costs[0]["total_spent"] or 0.0) if recent_costs else 0.0
    return {
        "user_id": user["id"],
        "username": user["username"],
        "total_cost": round(spent, 6),
        "total_prompt_tokens": (recent_costs[0]["total_p_tokens"] or 0) if recent_costs else 0,
        "total_completion_tokens": (recent_costs[0]["total_c_tokens"] or 0) if recent_costs else 0,
        "ai_enabled": bool(OPENAI_KEY),
        "default_model": OPENAI_MODEL
    }

@app.get("/api/health")
def health():
    return {"ai": bool(OPENAI_KEY), "model": OPENAI_MODEL}

@app.get("/api/notebooks")
def list_notebooks(user: dict = Depends(get_current_user)):
    nbs = db_query("SELECT id, title, created_at FROM notebooks WHERE user_id = ? ORDER BY id DESC", (user["id"],))
    if not nbs:
        nid = db_execute("INSERT INTO notebooks(user_id, title) VALUES (?, ?)", (user["id"], "My Notebook"))
        nbs = [{"id": nid, "title": "My Notebook"}]
    return nbs

@app.post("/api/notebooks", status_code=201)
def create_notebook(body: NotebookIn, user: dict = Depends(get_current_user)):
    nid = db_execute("INSERT INTO notebooks(user_id, title) VALUES (?, ?)", (user["id"], (body.title.strip() or "Untitled notebook")[:120]))
    return {"id": nid, "title": body.title}

@app.patch("/api/notebooks/{nid}")
def rename_notebook(nid: int, body: NotebookIn, user: dict = Depends(get_current_user)):
    db_execute("UPDATE notebooks SET title = ? WHERE id = ? AND user_id = ?", (body.title.strip()[:120] or "Untitled notebook", nid, user["id"]))
    return {"ok": True}

@app.delete("/api/notebooks/{nid}")
def delete_notebook(nid: int, user: dict = Depends(get_current_user)):
    db_execute("DELETE FROM notebooks WHERE id = ? AND user_id = ?", (nid, user["id"]))
    return {"ok": True}

def store_source_and_index(notebook_id: int, user_id: int, name: str, kind: str, text: str) -> Dict[str, Any]:
    emb_model = select_embedding_model(kind, text)
    sid = db_execute(
        "INSERT INTO sources(notebook_id, name, kind, text, enabled, embedding_model) VALUES (?, ?, ?, ?, 1, ?)",
        (notebook_id, name[:140], kind, text, emb_model)
    )
    raw_chunks = chunk_text(text, kind=kind)
    embeddings = []
    emb_cost = 0.0

    if OPENAI_KEY and emb_model.startswith("text-embedding"):
        try:
            embeddings, tokens, emb_cost = compute_openai_embedding(raw_chunks, emb_model)
            if emb_cost > 0:
                db_execute(
                    "INSERT INTO usage_logs(user_id, notebook_id, operation, model, prompt_tokens, cost_usd) VALUES (?, ?, 'embedding', ?, ?, ?)",
                    (user_id, notebook_id, emb_model, tokens, emb_cost)
                )
                db_execute("UPDATE users SET total_cost = total_cost + ? WHERE id = ?", (emb_cost, user_id))
        except Exception as e:
            print(f"Embedding error: {e}")
            embeddings = []

    for idx, chunk_content in enumerate(raw_chunks):
        emb_json = json.dumps(embeddings[idx]) if idx < len(embeddings) else None
        db_execute(
            "INSERT INTO chunks(source_id, chunk_index, content, embedding, tokens) VALUES (?, ?, ?, ?, ?)",
            (sid, idx, chunk_content, emb_json, len(chunk_content.split()))
        )

    return {
        "id": sid,
        "name": name,
        "kind": kind,
        "enabled": 1,
        "chars": len(text),
        "chunks_count": len(raw_chunks),
        "embedding_model": emb_model,
        "embedding_cost": emb_cost
    }

@app.get("/api/notebooks/{nid}/sources")
def list_sources(nid: int, user: dict = Depends(get_current_user)):
    return db_query(
        "SELECT id, name, kind, enabled, embedding_model, length(text) AS chars, created_at FROM sources WHERE notebook_id = ? ORDER BY id ASC",
        (nid,)
    )

@app.get("/api/sources/{sid}")
def get_source_details(sid: int, user: dict = Depends(get_current_user)):
    src = db_query("SELECT id, notebook_id, name, kind, text, enabled, embedding_model, created_at FROM sources WHERE id = ?", (sid,))
    if not src:
        raise HTTPException(404, "Source not found")
    chunks = db_query("SELECT id AS chunk_id, chunk_index, content FROM chunks WHERE source_id = ? ORDER BY chunk_index ASC", (sid,))
    res = dict(src[0])
    res["chunks"] = chunks
    return res

@app.post("/api/notebooks/{nid}/sources", status_code=201)
def paste_source(nid: int, body: TextSourceIn, user: dict = Depends(get_current_user)):
    if not body.text.strip():
        raise HTTPException(400, "Paste some text first.")
    return store_source_and_index(nid, user["id"], body.name or "Pasted note", body.kind or "text", body.text.strip())

@app.post("/api/notebooks/{nid}/sources/upload", status_code=201)
def upload_source(nid: int, file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    filename = file.filename or "uploaded_file"
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File exceeds maximum size of 16 MB.")

    kind = "file"
    text = ""
    lower = filename.lower()
    if lower.endswith(".pdf"):
        kind = "pdf"
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        text = "\n\n".join(p.extract_text() or "" for p in reader.pages)
    elif lower.endswith((".py", ".js", ".ts", ".html", ".css", ".json", ".sql", ".rs", ".go", ".cpp", ".java")):
        kind = "code"
        text = data.decode("utf-8", errors="replace")
    elif lower.endswith((".csv", ".tsv")):
        kind = "table"
        text = data.decode("utf-8", errors="replace")
    elif lower.endswith((".txt", ".md", ".log")):
        kind = "text"
        text = data.decode("utf-8", errors="replace")
    else:
        kind = "file"
        text = data.decode("utf-8", errors="replace")

    if not text.strip():
        raise HTTPException(400, "Could not extract readable text from that file.")

    return store_source_and_index(nid, user["id"], filename, kind, text.strip())

@app.post("/api/notebooks/{nid}/sources/url", status_code=201)
def url_source(nid: int, body: UrlIn, user: dict = Depends(get_current_user)):
    try:
        title, text = fetch_web_page(body.url.strip())
    except Exception as e:
        raise HTTPException(400, str(e))
    return store_source_and_index(nid, user["id"], title, "web", text)

@app.patch("/api/sources/{sid}")
def toggle_source(sid: int, body: EnabledIn, user: dict = Depends(get_current_user)):
    db_execute("UPDATE sources SET enabled = ? WHERE id = ?", (1 if body.enabled else 0, sid))
    return {"ok": True}

@app.delete("/api/sources/{sid}")
def delete_source(sid: int, user: dict = Depends(get_current_user)):
    db_execute("DELETE FROM sources WHERE id = ?", (sid,))
    return {"ok": True}

@app.get("/api/web-search")
def web_search(query: str = Query("", alias="q")):
    term = query.strip()
    if not term:
        return []
    try:
        r = requests.post("https://html.duckduckgo.com/html/", data={"q": term}, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        results = []
        for a in BeautifulSoup(r.text, "html.parser").select("a.result__a")[:8]:
            href = a.get("href", "")
            if "uddg=" in href:
                href = parse_qs(urlparse(href).query).get("uddg", [href])[0]
            results.append({"title": a.get_text(strip=True), "url": href})
        return results
    except Exception:
        raise HTTPException(502, "Web search is currently unavailable.")

@app.get("/api/notebooks/{nid}/apis")
def list_attached_apis(nid: int, user: dict = Depends(get_current_user)):
    return db_query("SELECT id, notebook_id, name, url, method, headers, description, enabled, created_at FROM attached_apis WHERE notebook_id = ?", (nid,))

@app.post("/api/notebooks/{nid}/apis", status_code=201)
def add_attached_api(nid: int, body: AttachedApiIn, user: dict = Depends(get_current_user)):
    aid = db_execute(
        "INSERT INTO attached_apis(notebook_id, name, url, method, headers, description, enabled) VALUES (?, ?, ?, ?, ?, ?, 1)",
        (nid, body.name.strip(), body.url.strip(), body.method.upper(), body.headers, body.description)
    )
    return {"id": aid, "name": body.name, "url": body.url}

@app.delete("/api/apis/{aid}")
def delete_attached_api(aid: int, user: dict = Depends(get_current_user)):
    db_execute("DELETE FROM attached_apis WHERE id = ?", (aid,))
    return {"ok": True}

@app.post("/api/apis/{aid}/test")
def test_attached_api(aid: int, params: Dict[str, Any] = {}, user: dict = Depends(get_current_user)):
    return execute_attached_api(aid, params)

@app.get("/api/notebooks/{nid}/messages")
def get_messages(nid: int, user: dict = Depends(get_current_user)):
    rows = db_query(
        "SELECT id, role, content, citations, target_source_id, cost_usd, prompt_tokens, completion_tokens, created_at FROM messages WHERE notebook_id = ? ORDER BY id ASC",
        (nid,)
    )
    for r in rows:
        try:
            r["citations"] = json.loads(r["citations"])
        except Exception:
            r["citations"] = []
    return rows

@app.post("/api/notebooks/{nid}/chat")
def chat_with_sources(nid: int, body: ChatIn, user: dict = Depends(get_current_user)):
    question = body.question.strip()
    if not question:
        raise HTTPException(400, "Please enter a question.")

    hits, strategy, ret_cost = retrieve_top_chunks(
        notebook_id=nid,
        query=question,
        target_source_id=body.target_source_id,
        k=5
    )

    if not hits and not body.highlighted_context:
        answer = "I couldn't find relevant content in your selected source(s). Try selecting another document or adding more notes."
        citations = []
        cost_usd = 0.0
        prompt_tok = 0
        comp_tok = 0
    else:
        citations = []
        context_parts = []

        if body.highlighted_context:
            context_parts.append(f"[Selection] Highlighted passage from document:\n{body.highlighted_context}")

        for idx, hit in enumerate(hits, 1):
            citations.append({
                "n": idx,
                "source_id": hit["source_id"],
                "source_name": hit["source_name"],
                "chunk_id": hit["chunk_id"],
                "chunk_index": hit["chunk_index"],
                "snippet": hit["content"][:160] + "..." if len(hit["content"]) > 160 else hit["content"]
            })
            context_parts.append(f"[{idx}] (Source: {hit['source_name']})\n{hit['content']}")

        context_str = "\n\n".join(context_parts)

        apis = db_query("SELECT id, name, description FROM attached_apis WHERE notebook_id = ? AND enabled = 1", (nid,))
        api_notice = ""
        if apis:
            api_notice = f"\nAvailable APIs connected to this notebook: {', '.join(a['name'] + ' (' + a['description'] + ')' for a in apis)}"

        if OPENAI_KEY:
            from openai import OpenAI
            client = OpenAI(api_key=OPENAI_KEY)

            history_rows = db_query(
                "SELECT role, content FROM messages WHERE notebook_id = ? ORDER BY id DESC LIMIT 6",
                (nid,)
            )[::-1]

            system_prompt = (
                "You are an AI research assistant. Your task is to answer questions strictly and accurately using the provided numbered sources.\n"
                "Format citations inline using square brackets like [1], [2] next to every claim or quote.\n"
                "If the sources do not contain the answer, explicitly state that the documents don't provide sufficient information.\n"
                f"{api_notice}"
            )

            messages_payload = [
                {"role": "system", "content": system_prompt},
                *[{"role": m["role"], "content": m["content"]} for m in history_rows],
                {"role": "user", "content": f"Document Sources:\n{context_str}\n\nQuestion: {question}"}
            ]

            try:
                chat_res = client.chat.completions.create(
                    model=OPENAI_MODEL,
                    messages=messages_payload,
                    temperature=0.2
                )
                answer = chat_res.choices[0].message.content
                prompt_tok = chat_res.usage.prompt_tokens
                comp_tok = chat_res.usage.completion_tokens
                
                p_rates = PRICING.get(OPENAI_MODEL, PRICING["gpt-4o-mini"])
                cost_usd = (prompt_tok * p_rates["input"]) + (comp_tok * p_rates["output"]) + ret_cost
            except Exception as e:
                raise HTTPException(502, f"OpenAI generation failed: {e}")
        else:
            cost_usd = 0.0
            prompt_tok = 0
            comp_tok = 0
            answer = "### Matching Passages (Local Mode):\n\n" + "\n\n".join(
                f"**[{c['n']}] {c['source_name']}:**\n> {hits[i]['content']}" for i, c in enumerate(citations)
            )

    db_execute(
        "INSERT INTO messages(notebook_id, user_id, role, content, target_source_id) VALUES (?, ?, 'user', ?, ?)",
        (nid, user["id"], question, body.target_source_id)
    )
    mid = db_execute(
        "INSERT INTO messages(notebook_id, user_id, role, content, citations, target_source_id, cost_usd, prompt_tokens, completion_tokens) VALUES (?, ?, 'assistant', ?, ?, ?, ?, ?, ?)",
        (nid, user["id"], answer, json.dumps(citations), body.target_source_id, cost_usd, prompt_tok, comp_tok)
    )

    if cost_usd > 0:
        db_execute(
            "INSERT INTO usage_logs(user_id, notebook_id, operation, model, prompt_tokens, completion_tokens, cost_usd) VALUES (?, ?, 'chat', ?, ?, ?, ?)",
            (user["id"], nid, OPENAI_MODEL, prompt_tok, comp_tok, cost_usd)
        )
        db_execute("UPDATE users SET total_cost = total_cost + ? WHERE id = ?", (cost_usd, user["id"]))

    return {
        "id": mid,
        "answer": answer,
        "citations": citations,
        "cost_usd": round(cost_usd, 6),
        "prompt_tokens": prompt_tok,
        "completion_tokens": comp_tok,
        "strategy": strategy,
        "model": OPENAI_MODEL if OPENAI_KEY else "Local Mode"
    }

@app.get("/api/usage/summary")
def get_usage_summary(user: dict = Depends(get_current_user)):
    logs = db_query(
        "SELECT id, operation, model, prompt_tokens, completion_tokens, cost_usd, created_at FROM usage_logs WHERE user_id = ? ORDER BY id DESC LIMIT 25",
        (user["id"],)
    )
    summary = db_query(
        "SELECT SUM(cost_usd) AS total_spent, SUM(prompt_tokens) AS total_prompt, SUM(completion_tokens) AS total_comp, COUNT(id) AS total_queries FROM usage_logs WHERE user_id = ?",
        (user["id"],)
    )
    return {
        "summary": summary[0] if summary else {},
        "recent_logs": logs
    }

app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
