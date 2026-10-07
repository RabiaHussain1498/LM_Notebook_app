# Notebook

A local NotebookLM app for uploading documents, chatting with them, and viewing source citations.

- **Backend:** FastAPI + SQLite
- **Frontend:** HTML, CSS, and JavaScript
- **Retrieval:** OpenAI embeddings
- **Answer generation:** OpenAI when an API key is configured

## Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn backend.main:app --reload
```

Open http://127.0.0.1:8000.

## Features

- Create, rename, switch, and delete notebooks
- Upload PDF, TXT, Markdown, and CSV files
- Store document text, chunks, embeddings, chat history, and costs
- Toggle or remove sources
- Ask questions with source citations
- Use OpenAI embeddings when `OPENAI_API_KEY` is provided

## Configuration

The optional environment variables are:

```env
OPENAI_API_KEY=your_api_key
OPENAI_MODEL=MODEL_NAME
DB_PATH=PATH_TO_DB
```

