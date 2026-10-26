# RAG System Analysis Notebooks

> **Stale / historical (2026-10-17):** It is a design document for a subsystem; the described service layout may not match the current code. Verify against `backend/src/` and `backend/openapi.json` before relying on it. Current contracts: [`docs/engineering/`](../../docs/engineering/README.md).

This directory contains Jupyter notebooks for analyzing the RAG system performance.

## Notebooks

### 1. `corpus_analysis.ipynb` - Entity/Corpus Analysis

Analyzes the extracted entities from the Knowledge Graph (Neo4j).

**Data Source:** `../datasets/export.csv` (real Neo4j export data)

**Sections:**

- Data Loading & Parsing
- Entity Overview (counts, types)
- Confidence Score Analysis
- Paper Coverage & ArXiv Categories
- Top Extracted Concepts
- Quality Assessment Summary

---

### 2. `rag_analysis_presentation.ipynb` - Query Performance Analysis

Deep dive analysis of RAG system query performance.

**Data Source:** Synthetic/simulated data (for demonstration)

**Sections:**

- EDA Highlights (Distributions & Correlations)
- Clustering Results (Query Intent Analysis)
- Classification Results (Relevance Prediction)
- Regression Results (Relevance Scoring)
- Feature Importance (Driver Analysis)
- Error Analysis (Failure Cases)

---

## Using Real Query Performance Data

To replace synthetic data with real query logs:

1. **Enable query logging** in the backend by adding metrics collection to the search endpoints
2. **Export logs** to CSV with these columns:
   - `query_id` — Unique identifier
   - `query_type` — 'Factoid', 'Reasoning', or 'Exploratory'
   - `semantic_score` — Cosine similarity from Vector DB (Qdrant)
   - `graph_score` — Path connectivity from Knowledge Graph (Neo4j)
   - `hybrid_score` — Weighted combination
   - `query_length` — Number of tokens
   - `latency_ms` — Processing time
   - `is_relevant` — Ground truth label (0 or 1)
3. **Modify the notebook** to load from CSV: `pd.read_csv('query_logs.csv')`

---

## Running the Notebooks

```bash
cd backend
jupyter notebook
```

Open either notebook and run all cells.
