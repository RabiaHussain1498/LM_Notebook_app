# LM Notebook App

Local **NotebookLM** built with **FastAPI**, **SQLite**, **ChromaDB**, and **Vanilla JS**. Upload multiple documents (PDF, TXT, Markdown, CSV), perform semantic vector searches, chat with AI, view inline citations, and highlight exact source passages in real-time.

---

## Features

- **Notebook Management**: Create, rename, switch, and delete distinct notebooks to organize your research.
- **Multi-Format Document Upload**: Support for PDF (`pypdf`), Plain Text (`.txt`), Markdown (`.md`), and CSV files.
- **Smart Semantic Chunking & Vector Search**:
  - Clean passage chunking optimized for document retrieval.
  - Hybrid storage with **ChromaDB** for vector similarity search and **SQLite** for relational data persistence.
- **Interactive Source Highlighting & Citations**:
  - Click on response citations to open and scroll directly to the source document.
  - Multi-strategy highlight search (exact match, prefix cleaning, substring windowing, and sentence matching) for smooth visualization.
- **Embedding & Query Cost Tracking**:
  - Displays token counts and estimated embedding costs per uploaded document.
  - Keeps track of total accumulated account usage.
- **Local & Responsive UI**: Clean, modern NotebookLM-inspired dark/light workspace with zero unnecessary frameworks.

---

## Architecture & Data Persistence

| Component | Storage File | Description |
|-----------|--------------|-------------|
| **Relational Database** | `notebook.db` | Single SQLite database serving as the main source of truth for users, notebooks, sources, chunks, chat history, and cost stats. |
| **Vector Database** | `chroma_db/` | ChromaDB vector index storing text embeddings for fast retrieval. Automatically synced from `notebook.db` on server startup. |

> **Note:** If `chroma_db/` is deleted, the server automatically rebuilds the vector store from `notebook.db` upon restart.

---

## Getting Started

### Prerequisites
- Python 3.9+
- OpenAI API Key

### Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/RabiaHussain1498/LM_Notebook_app.git
   cd LM_Notebook_app
   ```

2. **Create and activate a virtual environment:**
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Set up Environment Variables:**
   Copy `.env.example` to `.env` and enter your OpenAI API key:
   ```bash
   cp .env.example .env
   ```
   Edit `.env`:
   ```env
   OPENAI_API_KEY=your_openai_api_key_here
   OPENAI_MODEL=gpt-4o-mini
   DB_PATH=notebook.db
   ```

5. **Run the Application:**
   ```bash
   uvicorn backend.main:app --reload
   ```

6. **Open in Browser:**
   Navigate to [http://127.0.0.1:8000](http://127.0.0.1:8000).

---

## Project Structure

```
LM_Notebook_app/
├── backend/
│   ├── main.py           # FastAPI application, database logic, RAG pipeline, & endpoints
├── frontend/
│   ├── index.html        # Main app UI structure
│   ├── styles.css        # Custom CSS styles
│   └── app.js            # Frontend logic, API calls, dynamic UI updates & source highlighter
├── notebook.db           # SQLite database (auto-created)
├── chroma_db/            # ChromaDB vector store directory (auto-created)
├── requirements.txt      # Python dependencies
├── .env.example          # Environment variable template
└── README.md             # Documentation
```