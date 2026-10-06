# Notebook

A small NotebookLM-style app: add sources (pasted text, PDF/TXT/MD/CSV files, web pages), then ask questions answered from those sources with numbered citations.

- **Backend:** FastAPI + SQLite (`backend/main.py`)
- **Frontend:** plain HTML/CSS/JS in `frontend/` (no build step), served by the backend
- **Answers:** local TF-IDF passage retrieval. If `OPENAI_API_KEY` is set, the retrieved passages are sent to OpenAI to write the answer.

## Run

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # optional: add OPENAI_API_KEY
uvicorn backend.main:app --reload
```

Open http://127.0.0.1:8000. Interactive API docs: http://127.0.0.1:8000/docs.

## Features

- Multiple notebooks (create, rename, switch, delete); chat history is saved
- Add sources by paste, file upload/drag-drop, URL import, or web search (DuckDuckGo)
- Toggle or remove sources; only checked sources are used for answers
- Citations on every answer

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | `{ai: bool}` |
| GET/POST | `/api/notebooks` | list / create |
| PATCH/DELETE | `/api/notebooks/{id}` | rename / delete |
| GET/POST | `/api/notebooks/{id}/sources` | list / add pasted text |
| POST | `/api/notebooks/{id}/sources/upload` | file upload (`file`) |
| POST | `/api/notebooks/{id}/sources/url` | import a web page `{url}` |
| PATCH/DELETE | `/api/sources/{id}` | enable toggle `{enabled}` / delete |
| GET | `/api/web-search?q=` | search results |
| GET | `/api/notebooks/{id}/messages` | chat history |
| POST | `/api/notebooks/{id}/chat` | `{question}` → `{answer, citations}` |

## Structure

```
backend/main.py     FastAPI app, SQLite schema, retrieval, API
frontend/           index.html, style.css, app.js
requirements.txt  .env.example  .gitignore
```

## Notes

- Runs on localhost with no login; add authentication before exposing it publicly.
- URL import blocks private/loopback addresses.
- Web search scrapes DuckDuckGo's HTML page and may break if that page changes.
- Set `OPENAI_MODEL` in `.env` to change the model (default `gpt-4o-mini`).
