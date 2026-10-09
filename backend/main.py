"""
NotebookLM AI Workspace
"""
import collections
import io
import ipaddress
import json
import math
import os
import re
import socket
import sqlite3
import time
from contextlib import asynccontextmanager, closing
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import chromadb
from chromadb.config import Settings
import requests
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()
DB = os.environ.get("DB_PATH", "notebook.db")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-6-luna").strip()
CHROMA_DIR = os.environ.get("CHROMA_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "chroma_db"))
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 MB

# Initialize Persistent ChromaDB Client & Collection
chroma_client = chromadb.PersistentClient(path=os.path.abspath(CHROMA_DIR))
rag_collection = chroma_client.get_or_create_collection(
    name="notebook_rag",
    metadata={"hnsw:space": "cosine"}
)

# Pricing constants (USD per token)
PRICING = {
    "gpt-4o-mini": {"input": 0.150 / 1_000_000, "output": 0.600 / 1_000_000},
    "gpt-6-luna": {"input": 0.100 / 1_000_000, "output": 0.500 / 1_000_000},
    "gpt-4o": {"input": 2.500 / 1_000_000, "output": 10.000 / 1_000_000},
    "text-embedding-3-small": {"input": 0.020 / 1_000_000, "output": 0.0},
    "text-embedding-3-large": {"input": 0.130 / 1_000_000, "output": 0.0},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    total_cost REAL DEFAULT 0.0
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
    embedding_cost REAL DEFAULT 0.0,
    folder_path TEXT NOT NULL DEFAULT '',
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

def migrate_db(c):
    cur = c.cursor()

    user_cols = [r[1] for r in cur.execute("PRAGMA table_info(users)").fetchall()]
    if "password_hash" in user_cols or "salt" in user_cols:
        cur.execute("PRAGMA foreign_keys=OFF")
        cur.execute("DROP TABLE IF EXISTS sessions")
        cur.execute("CREATE TABLE users_new (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, total_cost REAL DEFAULT 0.0)")
        cur.execute(
            "INSERT INTO users_new(id, username, created_at, total_cost) "
            "SELECT id, username, created_at, total_cost FROM users"
        )
        cur.execute("DROP TABLE users")
        cur.execute("ALTER TABLE users_new RENAME TO users")
        cur.execute("PRAGMA foreign_keys=ON")
        c.commit()

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
    if "embedding_cost" not in cols:
        cur.execute("ALTER TABLE sources ADD COLUMN embedding_cost REAL DEFAULT 0.0")
    if "folder_path" not in cols:
        cur.execute("ALTER TABLE sources ADD COLUMN folder_path TEXT NOT NULL DEFAULT ''")
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

def get_demo_user() -> dict:
    users = db_query("SELECT id, username, total_cost FROM users WHERE username = 'demo_user'")
    if users:
        return users[0]

    uid = db_execute("INSERT INTO users(username) VALUES (?)", ("demo_user",))
    return {"id": uid, "username": "demo_user", "total_cost": 0.0}

def sync_sqlite_to_chroma():
    """Sync any existing chunks from SQLite to ChromaDB on startup"""
    try:
        raw_chunks = db_query("""
            SELECT c.id AS chunk_id, c.chunk_index, c.content, c.embedding, s.id AS source_id, s.name AS source_name, s.kind, s.notebook_id
            FROM chunks c
            JOIN sources s ON c.source_id = s.id
        """)
        if not raw_chunks:
            return
        
        ids = []
        docs = []
        metas = []
        embeddings = []
        has_embs = True

        for r in raw_chunks:
            ids.append(f"c_{r['chunk_id']}")
            docs.append(r["content"])
            metas.append({
                "notebook_id": int(r["notebook_id"]),
                "source_id": int(r["source_id"]),
                "source_name": str(r["source_name"]),
                "chunk_id": int(r["chunk_id"]),
                "chunk_index": int(r["chunk_index"]),
                "kind": str(r["kind"] or "text")
            })
            if r["embedding"]:
                try:
                    embeddings.append(json.loads(r["embedding"]))
                except Exception:
                    has_embs = False
            else:
                has_embs = False

        if has_embs and len(embeddings) == len(ids):
            rag_collection.upsert(ids=ids, embeddings=embeddings, documents=docs, metadatas=metas)
        else:
            rag_collection.upsert(ids=ids, documents=docs, metadatas=metas)
    except Exception as e:
        print(f"Chroma startup sync notice: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    with closing(sqlite3.connect(DB)) as c:
        c.executescript(SCHEMA)
        migrate_db(c)
    get_demo_user()
    sync_sqlite_to_chroma()
    yield

app = FastAPI(title="NotebookLM AI App", lifespan=lifespan)

def current_user() -> dict:
    return get_demo_user()

def check_notebook_owner(notebook_id: int, user_id: int):
    nb = db_query("SELECT id FROM notebooks WHERE id = ? AND user_id = ?", (notebook_id, user_id))
    if not nb:
        raise HTTPException(404, "Notebook not found or access denied.")

def check_source_owner(source_id: int, user_id: int) -> dict:
    src = db_query(
        "SELECT s.* FROM sources s JOIN notebooks n ON s.notebook_id = n.id WHERE s.id = ? AND n.user_id = ?",
        (source_id, user_id)
    )
    if not src:
        raise HTTPException(404, "Source not found or access denied.")
    return src[0]

STOPWORDS = set("the a an is are was were of to in on and or for with what how why who when which do does did it this that be as at by from about me my you your tell can i we us they them he she had have has".split())

def clean_pdf_text(text: str) -> str:
    """
    Fix common pypdf extraction artifacts:
    - Normalize non-breaking spaces (\xa0) and unicode spaces
    - Dehyphenate broken words across lines (impres-\nsive -> impressive)
    - Remove standalone watermark / ebook footer lines
    - Remove isolated page numbers
    - Normalize unicode dashes and quotes
    """
    text = text.replace("\xa0", " ").replace("\u200b", "")
    # Merge hyphenated line-breaks: "impres-\nsive" -> "impressive"
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    # Remove standalone watermark / ebook URL / footer lines
    text = re.sub(r"(?m)^\s*(?:www\.[a-z0-9\-]+\.[a-z]{2,4}|page\s*\d+|\d{1,4})\s*$", "", text)
    # Collapse small runs of 2-3 spaces (extraction artifacts) but preserve
    # 4+ space runs (table column alignment) and tabs
    text = re.sub(r"(?<!\S)[ ]{2,3}(?!\S[ ])", " ", text)
    # Normalize dashes and quotes to ASCII
    text = text.replace("\u2013", "-").replace("\u2014", "-")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    # Strip trailing spaces on each line
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return text.strip()

def is_heading_or_section_start(text: str) -> bool:
    first_line = text.split("\n")[0].strip()
    if re.match(r"^(#{1,6}\s+|[A-Z][A-Za-z0-9\s]{1,40}:|\d+[\.\)]\s+|(?:Q|Question|Checkpoint|Exercise|Problem|Task|Day|Part|Section|Chapter|About|To\s+my|Author|Copyright|Dedication|Introduction|Conclusion|References)\b)", first_line, re.I):
        return True
    if len(first_line) < 45 and re.match(r"^[A-Z][a-z]+(?:\s+[A-Za-z0-9\(\)]+){0,5}$", first_line):
        return True
    return False

def tokenize(text: str) -> List[str]:
    return [w for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in STOPWORDS and len(w) > 1]

def chunk_markdown_or_text(text: str, target_size: int = 600, overlap: int = 0) -> List[str]:
    """
    Clean Semantic + Structural Chunking for Prose, PDFs, Markdown:
    - Splits on section boundaries, markdown headers, questions, numbered lists, bullet points, and paragraph breaks.
    - Preserves logical units (e.g., individual questions/answers, dedications, bios, or list items) intact.
    - If a unit starts with a heading or represents an independent block, it forms its own clean chunk.
    - If an individual unit exceeds target_size, splits strictly on sentence boundaries.
    """
    normalized = re.sub(r"\r\n|\r", "\n", text).strip()
    if not normalized:
        return []

    pattern = r"\n\s*\n|\n(?=(?:(?:\d+|[a-zA-Z])[\.\)]\s+|(?:Q|Question|Checkpoint|Exercise|Problem|Task|Day|Part|Section|Chapter|About|To\s+my|Author|Dedication|Introduction|Conclusion)\b|#{1,6}\s+|[-*•]\s+|[A-Z][A-Za-z0-9\s]{1,35}:|[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,4}\s*\n))"
    units = [u.strip() for u in re.split(pattern, normalized) if u.strip()]

    chunks_out: List[str] = []
    current_chunk = ""

    for unit in units:
        is_header = is_heading_or_section_start(unit)

        # Flush previous chunk if this unit is a distinct section or header
        if is_header and current_chunk:
            chunks_out.append(current_chunk.strip())
            current_chunk = ""

        if len(unit) > target_size:
            if current_chunk:
                chunks_out.append(current_chunk.strip())
                current_chunk = ""
            sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", unit) if s.strip()]
            for sentence in sentences:
                if current_chunk and len(current_chunk) + 1 + len(sentence) > target_size:
                    chunks_out.append(current_chunk.strip())
                    current_chunk = sentence
                else:
                    current_chunk += ("\n\n" if current_chunk else "") + sentence
        else:
            # If current_chunk has substantial content (> 220 chars) or adding unit exceeds target_size, flush
            if current_chunk and (len(current_chunk) > 220 or len(current_chunk) + 2 + len(unit) > target_size):
                chunks_out.append(current_chunk.strip())
                current_chunk = unit
            else:
                current_chunk += ("\n\n" if current_chunk else "") + unit

    if current_chunk.strip():
        chunks_out.append(current_chunk.strip())

    return chunks_out or [normalized[:target_size]]

def chunk_code(text: str, target_size: int = 600, overlap_lines: int = 2) -> List[str]:
    """
    Structural Code Chunking:
    - Splits along class, function, or block boundaries.
    - Preserves indentation and syntax integrity.
    """
    lines = text.split("\n")
    chunks_out = []
    current_lines = []
    
    block_start_regex = re.compile(r"^\s*(def |class |async def |function |export |public |private |struct |impl |SELECT |CREATE |INSERT )")
    
    for line in lines:
        if block_start_regex.match(line) and current_lines and sum(len(l) for l in current_lines) >= target_size // 2:
            chunks_out.append("\n".join(current_lines).strip())
            current_lines = current_lines[-overlap_lines:] if len(current_lines) > overlap_lines else []
        
        current_lines.append(line)
        if sum(len(l) for l in current_lines) > target_size:
            chunks_out.append("\n".join(current_lines).strip())
            current_lines = current_lines[-overlap_lines:] if len(current_lines) > overlap_lines else []

    if current_lines:
        chunk_str = "\n".join(current_lines).strip()
        if chunk_str:
            chunks_out.append(chunk_str)

    return chunks_out or [text[:target_size]]

def chunk_tabular(text: str, target_size: int = 550) -> List[str]:
    """
    Schema-Aware Tabular Chunking for CSV/TSV:
    - Extracts header row and prefixes each chunk with column names so embeddings understand the schema.
    """
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if not lines:
        return []
    
    header = lines[0]
    data_rows = lines[1:]
    if not data_rows:
        return [header]
        
    chunks_out = []
    current_chunk_rows = []
    
    for row in data_rows:
        projected_size = len(header) + sum(len(r) + 1 for r in current_chunk_rows) + len(row)
        if current_chunk_rows and projected_size > target_size:
            chunks_out.append(f"[Columns: {header}]\n" + "\n".join(current_chunk_rows))
            current_chunk_rows = []
        current_chunk_rows.append(row)
        
    if current_chunk_rows:
        chunks_out.append(f"[Columns: {header}]\n" + "\n".join(current_chunk_rows))
        
    return chunks_out

def chunk_text(text: str, kind: str = "text", size: int = 550, overlap: int = 70) -> List[str]:
    """Master Hybrid Structural + Semantic Chunking Router"""
    if kind == "code":
        return chunk_code(text, target_size=size)
    elif kind == "table":
        return chunk_tabular(text, target_size=size)
    else:
        return chunk_markdown_or_text(text, target_size=size, overlap=overlap)

def select_embedding_model(kind: str, text: str) -> str:
    if not OPENAI_KEY:
        return "tf-idf-local"
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
    strategy_used = "chromadb-vector"

    # 1. Primary retrieval: ChromaDB Vector Similarity Search
    if OPENAI_KEY:
        try:
            emb_model = "text-embedding-3-small"
            q_embeddings, tokens, cost = compute_openai_embedding([query], emb_model)
            retrieval_cost += cost
            strategy_used = f"ChromaDB ({emb_model})"

            enabled_sids = list(set(int(r["source_id"]) for r in raw_chunks))
            if len(enabled_sids) == 1:
                where_filter = {
                    "$and": [
                        {"notebook_id": {"$eq": int(notebook_id)}},
                        {"source_id": {"$eq": int(enabled_sids[0])}}
                    ]
                }
            else:
                where_filter = {
                    "$and": [
                        {"notebook_id": {"$eq": int(notebook_id)}},
                        {"source_id": {"$in": [int(sid) for sid in enabled_sids]}}
                    ]
                }

            chroma_count = rag_collection.count()
            if chroma_count > 0:
                query_res = rag_collection.query(
                    query_embeddings=q_embeddings,
                    n_results=min(k * 2, chroma_count),
                    where=where_filter
                )
                
                if query_res and query_res.get("ids") and query_res["ids"][0]:
                    hits = []
                    res_ids = query_res["ids"][0]
                    res_docs = query_res["documents"][0]
                    res_metas = query_res["metadatas"][0]
                    res_dists = query_res.get("distances", [[]])[0] or [0.0] * len(res_ids)

                    for cid_str, doc_text, meta, dist in zip(res_ids, res_docs, res_metas, res_dists):
                        score = round(1.0 - dist, 4) if dist is not None else 1.0
                        if score > 0.15:
                            raw_cid = meta.get("chunk_id")
                            if raw_cid is None:
                                raw_cid = int(cid_str.replace("c_", "")) if str(cid_str).startswith("c_") else int(cid_str)
                            hits.append({
                                "chunk_id": int(raw_cid),
                                "chunk_index": meta.get("chunk_index", 0),
                                "content": doc_text,
                                "source_id": meta.get("source_id"),
                                "source_name": meta.get("source_name", "Source"),
                                "kind": meta.get("kind", "text"),
                                "score": score
                            })

                    if hits:
                        return hits[:k], strategy_used, retrieval_cost
        except Exception as e:
            print(f"ChromaDB query fallback: {e}")

    # 2. Local TF-IDF / Overview fallback
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

# Pydantic models
class NotebookIn(BaseModel):
    title: str = "Untitled notebook"

class TextSourceIn(BaseModel):
    name: str = "Pasted note"
    text: str
    kind: str = "text"

class EnabledIn(BaseModel):
    enabled: bool

class ChatIn(BaseModel):
    question: str
    target_source_id: Optional[int] = None
    highlighted_context: Optional[str] = None

# API Endpoints
@app.get("/api/health")
def health():
    chroma_status = "ok"
    chroma_count = 0
    try:
        chroma_count = rag_collection.count()
    except Exception as e:
        chroma_status = f"error: {e}"
        
    return {
        "status": "ok",
        "ai": bool(OPENAI_KEY),
        "model": OPENAI_MODEL,
        "chroma": {
            "status": chroma_status,
            "total_vectors": chroma_count
        }
    }

@app.get("/api/notebooks")
def list_notebooks(user: dict = Depends(current_user)):
    nbs = db_query("SELECT id, title, created_at FROM notebooks WHERE user_id = ? ORDER BY id DESC", (user["id"],))
    if not nbs:
        nid = db_execute("INSERT INTO notebooks(user_id, title) VALUES (?, ?)", (user["id"], "My Notebook"))
        nbs = [{"id": nid, "title": "My Notebook"}]
    return nbs

@app.post("/api/notebooks", status_code=201)
def create_notebook(body: NotebookIn, user: dict = Depends(current_user)):
    nid = db_execute("INSERT INTO notebooks(user_id, title) VALUES (?, ?)", (user["id"], (body.title.strip() or "Untitled notebook")[:120]))
    return {"id": nid, "title": body.title}

@app.get("/api/notebooks/{nid}/cost")
def get_notebook_cost(nid: int, user: dict = Depends(current_user)):
    check_notebook_owner(nid, user["id"])
    costs = db_query(
        "SELECT SUM(cost_usd) as total_cost, SUM(prompt_tokens) as prompt_tokens, SUM(completion_tokens) as completion_tokens, COUNT(id) as total_queries FROM messages WHERE notebook_id = ? AND role = 'assistant'",
        (nid,)
    )
    emb_costs = db_query(
        "SELECT SUM(cost_usd) as emb_cost FROM usage_logs WHERE notebook_id = ? AND operation = 'embedding'",
        (nid,)
    )
    total_msg_cost = (costs[0]["total_cost"] or 0.0) if costs else 0.0
    total_emb_cost = (emb_costs[0]["emb_cost"] or 0.0) if emb_costs else 0.0
    total_spent = total_msg_cost + total_emb_cost
    
    return {
        "notebook_id": nid,
        "total_cost": round(total_spent, 6),
        "prompt_tokens": (costs[0]["prompt_tokens"] or 0) if costs else 0,
        "completion_tokens": (costs[0]["completion_tokens"] or 0) if costs else 0,
        "total_queries": (costs[0]["total_queries"] or 0) if costs else 0
    }

@app.patch("/api/notebooks/{nid}")
def rename_notebook(nid: int, body: NotebookIn, user: dict = Depends(current_user)):
    check_notebook_owner(nid, user["id"])
    db_execute("UPDATE notebooks SET title = ? WHERE id = ? AND user_id = ?", (body.title.strip()[:120] or "Untitled notebook", nid, user["id"]))
    return {"ok": True}

@app.delete("/api/notebooks/{nid}")
def delete_notebook(nid: int, user: dict = Depends(current_user)):
    check_notebook_owner(nid, user["id"])
    try:
        rag_collection.delete(where={"notebook_id": {"$eq": int(nid)}})
    except Exception as e:
        print(f"Chroma delete notebook notice: {e}")
    db_execute("DELETE FROM notebooks WHERE id = ? AND user_id = ?", (nid, user["id"]))
    return {"ok": True}

def store_source_and_index(
    notebook_id: int,
    user_id: int,
    name: str,
    kind: str,
    text: str,
    folder_path: str = "",
) -> Dict[str, Any]:
    emb_model = select_embedding_model(kind, text)
    sid = db_execute(
        "INSERT INTO sources(notebook_id, name, kind, text, enabled, embedding_model, embedding_cost, folder_path) VALUES (?, ?, ?, ?, 1, ?, 0.0, ?)",
        (notebook_id, name[:140], kind, text, emb_model, folder_path)
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
                db_execute("UPDATE sources SET embedding_cost = ? WHERE id = ?", (emb_cost, sid))
        except Exception as e:
            print(f"Embedding error: {e}")
            embeddings = []

    chunk_ids = []
    for idx, chunk_content in enumerate(raw_chunks):
        emb_json = json.dumps(embeddings[idx]) if idx < len(embeddings) else None
        cid = db_execute(
            "INSERT INTO chunks(source_id, chunk_index, content, embedding, tokens) VALUES (?, ?, ?, ?, ?)",
            (sid, idx, chunk_content, emb_json, len(chunk_content.split()))
        )
        chunk_ids.append(cid)

    # Upsert chunks and embeddings into ChromaDB collection
    try:
        chroma_ids = [f"c_{cid}" for cid in chunk_ids]
        chroma_metas = [{
            "notebook_id": int(notebook_id),
            "source_id": int(sid),
            "source_name": str(name),
            "chunk_id": int(cid),
            "chunk_index": int(idx),
            "kind": str(kind)
        } for idx, cid in enumerate(chunk_ids)]

        if embeddings and len(embeddings) == len(raw_chunks):
            rag_collection.upsert(
                ids=chroma_ids,
                embeddings=embeddings,
                documents=raw_chunks,
                metadatas=chroma_metas
            )
        else:
            rag_collection.upsert(
                ids=chroma_ids,
                documents=raw_chunks,
                metadatas=chroma_metas
            )
    except Exception as e:
        print(f"ChromaDB upsert notice: {e}")

    return {
        "id": sid,
        "name": name,
        "kind": kind,
        "enabled": 1,
        "chars": len(text),
        "chunks_count": len(raw_chunks),
        "embedding_model": emb_model,
        "embedding_cost": emb_cost,
        "folder_path": folder_path
    }

@app.get("/api/notebooks/{nid}/sources")
def list_sources(nid: int, user: dict = Depends(current_user)):
    check_notebook_owner(nid, user["id"])
    return db_query(
        """SELECT s.id, s.name, s.kind, s.enabled, s.embedding_model, s.folder_path,
                  length(s.text) AS chars, s.created_at,
                  COALESCE(s.embedding_cost, 0.0) AS embedding_cost,
                  COUNT(c.id) AS chunks_count
           FROM sources s
           LEFT JOIN chunks c ON s.id = c.source_id
           WHERE s.notebook_id = ?
           GROUP BY s.id
           ORDER BY s.id ASC""",
        (nid,)
    )

@app.get("/api/sources/{sid}")
def get_source_details(sid: int, user: dict = Depends(current_user)):
    src = check_source_owner(sid, user["id"])
    chunks = db_query("SELECT id AS chunk_id, chunk_index, content FROM chunks WHERE source_id = ? ORDER BY chunk_index ASC", (sid,))
    res = dict(src)
    res["chunks"] = chunks
    res["chunks_count"] = len(chunks)
    return res

@app.post("/api/notebooks/{nid}/sources", status_code=201)
def paste_source(nid: int, body: TextSourceIn, user: dict = Depends(current_user)):
    check_notebook_owner(nid, user["id"])
    if not body.text.strip():
        raise HTTPException(400, "Paste some text first.")
    return store_source_and_index(nid, user["id"], body.name or "Pasted note", body.kind or "text", body.text.strip())

@app.post("/api/notebooks/{nid}/sources/upload", status_code=201)
def upload_source(
    nid: int,
    file: UploadFile = File(...),
    folder_path: str = Form(""),
    user: dict = Depends(current_user),
):
    check_notebook_owner(nid, user["id"])
    filename = (file.filename or "uploaded_file").replace("\\", "/").rsplit("/", 1)[-1]
    folder_parts = [
        part for part in folder_path.replace("\\", "/").split("/")
        if part not in ("", ".")
    ]
    if any(part == ".." for part in folder_parts):
        raise HTTPException(400, "Invalid folder path.")
    normalized_folder_path = "/".join(folder_parts)[:500]
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File exceeds maximum size of 200 MB.")

    kind = "file"
    text = ""
    lower = filename.lower()
    if lower.endswith(".pdf"):
        kind = "pdf"
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        raw = "\n\n".join(p.extract_text() or "" for p in reader.pages)
        text = clean_pdf_text(raw)
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

    return store_source_and_index(
        nid, user["id"], filename, kind, text.strip(), normalized_folder_path
    )

@app.patch("/api/sources/{sid}")
def toggle_source(sid: int, body: EnabledIn, user: dict = Depends(current_user)):
    check_source_owner(sid, user["id"])
    db_execute("UPDATE sources SET enabled = ? WHERE id = ?", (1 if body.enabled else 0, sid))
    return {"ok": True}

@app.delete("/api/sources/{sid}")
def delete_source(sid: int, user: dict = Depends(current_user)):
    check_source_owner(sid, user["id"])
    try:
        rag_collection.delete(where={"source_id": {"$eq": int(sid)}})
    except Exception as e:
        print(f"Chroma delete source notice: {e}")
    db_execute("DELETE FROM sources WHERE id = ?", (sid,))
    return {"ok": True}

@app.get("/api/notebooks/{nid}/messages")
def get_messages(nid: int, user: dict = Depends(current_user)):
    check_notebook_owner(nid, user["id"])
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

# Safety Guardrails
SELF_HARM_REGEX = re.compile(
    r"\b(suicide|suicidal|kill\s+(?:my\s*self|me)|end\s+my\s+life|self\s*harm|cutting\s+(?:my\s*self|me)|hang\s+(?:my\s*self|me)|want\s+to\s+die|how\s+to\s+die|commit\s+suicide|take\s+my\s+own\s+life)\b",
    re.IGNORECASE
)

VIOLENCE_HARM_REGEX = re.compile(
    r"\b(murder|assassinate|assassination|massacre|terrorist|terrorism|how\s+to\s+kill|ways\s+to\s+kill|instructions?\s+to\s+kill|kill\s+(?:someone|somebody|a\s+person|people|others|anyone)|hire\s+a\s+hitman|how\s+to\s+make\s+a\s+bomb|manufacture\s+explosives|school\s+shooting|build\s+a\s+weapon|poison\s+(?:someone|somebody|a\s+person|people))\b",
    re.IGNORECASE
)

# Conversational Queries (Greetings / Identity)
GREETING_REGEX = re.compile(
    r"^\s*(hi|hy|hello|hey|heyy|greetings|good\s+morning|good\s+afternoon|good\s+evening|howdy|sup|yo|hola)\b[!\?.,\s]*$",
    re.IGNORECASE
)

CAPABILITY_REGEX = re.compile(
    r"^\s*(who\s+are\s+you|what\s+are\s+you|what\s+can\s+you\s+do|how\s+can\s+you\s+help|what\s+is\s+this\s+app|help\s*me|how\s+does\s+this\s+work|what\s+is\s+notebooklm)\b[!\?.,\s]*$",
    re.IGNORECASE
)

def normalize_citation_tags(text: str) -> str:
    """Expand citation ranges and lists like [1]–[2] or [1, 2] to standard [1] [2]"""
    def expand_range(match):
        start = int(match.group(1))
        end = int(match.group(2))
        if 1 <= start <= end and end - start <= 10:
            return " ".join(f"[{i}]" for i in range(start, end + 1))
        return match.group(0)

    text = re.sub(r"\[(\d+)\s*[-–—]\s*(\d+)\]", expand_range, text)
    text = re.sub(r"\[(\d+)\]\s*[-–—]\s*\[(\d+)\]", expand_range, text)
    text = re.sub(r"\[(\d+(?:\s*,\s*\d+)+)\]", lambda m: " ".join(f"[{x.strip()}]" for x in m.group(1).split(",")), text)
    return text

@app.post("/api/notebooks/{nid}/chat")
def chat_with_sources(nid: int, body: ChatIn, user: dict = Depends(current_user)):
    check_notebook_owner(nid, user["id"])
    t0 = time.time()
    question = body.question.strip()
    if not question:
        raise HTTPException(400, "Please enter a question.")

    # 1. Safety Guardrail: Self-harm / Suicide
    if SELF_HARM_REGEX.search(question):
        answer = (
            "If you or someone you know is going through a difficult time or having thoughts of self-harm or suicide, "
            "please know that you are not alone and support is available:\n\n"
            "• **In the US & Canada:** Call or text **988** (Suicide & Crisis Lifeline)\n"
            "• **In the UK:** Call **111** or text **SHOUT** to **85258**\n"
            "• **International Resources:** Visit [befrienders.org](https://www.befrienders.org) or [iasp.info/resources/Crisis_Centres](https://www.iasp.info/resources/Crisis_Centres/)\n\n"
            "Please reach out to a trusted person, counselor, or emergency services."
        )
        citations = []
        cost_usd = 0.0
        prompt_tok = 0
        comp_tok = 0
        strategy = "safety-guardrail"

    # 2. Safety Guardrail: Violence / Harm / Illegal Acts
    elif VIOLENCE_HARM_REGEX.search(question):
        answer = (
            "I cannot fulfill this request. I am designed to assist with research and note analysis, "
            "and I cannot generate content or provide guidance related to violence, murder, self-harm, or illegal acts."
        )
        citations = []
        cost_usd = 0.0
        prompt_tok = 0
        comp_tok = 0
        strategy = "safety-guardrail"

    # 3. Conversational Queries: Greetings
    elif GREETING_REGEX.match(question):
        answer = "Hello! How can I help you with your notes and research today? You can select documents on the left and ask questions, generate summaries, or explore specific topics."
        citations = []
        cost_usd = 0.0
        prompt_tok = 0
        comp_tok = 0
        strategy = "conversational-greeting"

    # 4. Conversational Queries: Identity & Capabilities
    elif CAPABILITY_REGEX.match(question):
        answer = (
            "I am your NotebookLM AI assistant! I help you analyze, search, and synthesize information from your uploaded sources and notes.\n\n"
            "Here is what you can do:\n"
            "• **Upload Documents**: Add PDFs, Markdown, Word, PowerPoint, Code, or Text files in the left sidebar.\n"
            "• **Ask Questions**: Ask anything grounded in your documents with precise source citations.\n"
            "• **Highlight & Jump**: Click on any citation to open and highlight the exact passage in your document."
        )
        citations = []
        cost_usd = 0.0
        prompt_tok = 0
        comp_tok = 0
        strategy = "conversational-capability"

    # 5. Standard RAG Research Query
    else:
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
            raw_citations = []
            context_parts = []

            if body.highlighted_context:
                context_parts.append(f"[Selection] Highlighted passage from document:\n{body.highlighted_context}")

            for idx, hit in enumerate(hits, 1):
                raw_citations.append({
                    "n": idx,
                    "source_id": hit["source_id"],
                    "source_name": hit["source_name"],
                    "chunk_id": hit["chunk_id"],
                    "chunk_index": hit["chunk_index"],
                    "content": hit["content"],
                    "snippet": hit["content"][:160] + "..." if len(hit["content"]) > 160 else hit["content"]
                })
                context_parts.append(f"[{idx}] (Source: {hit['source_name']})\n{hit['content']}")

            context_str = "\n\n".join(context_parts)

            if OPENAI_KEY:
                from openai import OpenAI
                client = OpenAI(api_key=OPENAI_KEY)

                history_rows = db_query(
                    "SELECT role, content FROM messages WHERE notebook_id = ? ORDER BY id DESC LIMIT 6",
                    (nid,)
                )[::-1]

                system_prompt = (
                    "You are an AI research assistant. Your task is to answer questions strictly, clearly, and accurately using the provided numbered document sources.\n"
                    "Citation Rules:\n"
                    "- Format citations inline using square brackets like [1] or [2] immediately following each specific claim or quote.\n"
                    "- Always cite individual sources separately (e.g. write '[1] [2]', NEVER combine into ranges like '[1-2]').\n"
                    "- Only reference a source number [N] if the fact is directly supported by that specific document snippet.\n"
                    "- If the provided sources do not contain sufficient information to answer the question, clearly state that the sources do not provide the information."
                )

                messages_payload = [
                    {"role": "system", "content": system_prompt},
                    *[{"role": m["role"], "content": m["content"]} for m in history_rows],
                    {"role": "user", "content": f"Document Sources:\n{context_str}\n\nQuestion: {question}"}
                ]

                try:
                    chat_kwargs = {
                        "model": OPENAI_MODEL,
                        "messages": messages_payload,
                    }
                    if not (OPENAI_MODEL.startswith("o1") or OPENAI_MODEL.startswith("o3") or "luna" in OPENAI_MODEL):
                        chat_kwargs["temperature"] = 0.2

                    try:
                        chat_res = client.chat.completions.create(**chat_kwargs)
                    except Exception as api_err:
                        if "temperature" in str(api_err).lower():
                            chat_kwargs.pop("temperature", None)
                            chat_res = client.chat.completions.create(**chat_kwargs)
                        else:
                            raise api_err

                    raw_answer = chat_res.choices[0].message.content or ""
                    prompt_tok = chat_res.usage.prompt_tokens if chat_res.usage else 0
                    comp_tok = chat_res.usage.completion_tokens if chat_res.usage else 0
                    
                    p_rates = PRICING.get(OPENAI_MODEL, PRICING.get("gpt-4o-mini", {"input": 0.150 / 1_000_000, "output": 0.600 / 1_000_000}))
                    cost_usd = (prompt_tok * p_rates["input"]) + (comp_tok * p_rates["output"]) + ret_cost

                    # Normalize citation tags and re-index sequentially
                    normalized_answer = normalize_citation_tags(raw_answer)
                    old_to_new = {}
                    used_citations = []
                    for m in re.finditer(r"\[(\d+)\]", normalized_answer):
                        old_n = int(m.group(1))
                        if old_n not in old_to_new and 1 <= old_n <= len(raw_citations):
                            new_n = len(old_to_new) + 1
                            old_to_new[old_n] = new_n
                            orig = raw_citations[old_n - 1].copy()
                            orig["n"] = new_n
                            used_citations.append(orig)

                    if old_to_new:
                        answer = re.sub(r"\[(\d+)\]", lambda m: f"[{old_to_new.get(int(m.group(1)), m.group(1))}]", normalized_answer)
                        citations = used_citations
                    else:
                        answer = normalized_answer
                        citations = raw_citations if re.search(r"\[\d+\]", answer) else []

                except Exception as e:
                    raise HTTPException(502, f"OpenAI generation failed: {e}")
            else:
                cost_usd = 0.0
                prompt_tok = 0
                comp_tok = 0
                citations = raw_citations
                answer = "### Matching Passages (Local Mode):\n\n" + "\n\n".join(
                    f"**[{c['n']}] {c['source_name']}:**\n> {hits[i]['content']}" for i, c in enumerate(citations)
                )

    latency_s = round(time.time() - t0, 2)

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
        "latency_s": latency_s,
        "prompt_tokens": prompt_tok,
        "completion_tokens": comp_tok,
        "total_tokens": prompt_tok + comp_tok,
        "strategy": strategy,
        "model": OPENAI_MODEL if OPENAI_KEY else "Local Mode"
    }

@app.get("/api/usage/summary")
def get_usage_summary(user: dict = Depends(current_user)):
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
