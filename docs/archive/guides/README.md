# Archived guides

Setup, implementation and quick-start guides retired from [`docs/guides/`](../../guides/README.md) on 2026-10-17. They describe an earlier system (Qdrant vector search, a MUI frontend, CrewAI agents, early realtime-processing modules) and many of the files, scripts and commands they cite no longer exist. Their text is preserved unchanged as history. Do not follow their commands; use [current engineering contracts](../../engineering/README.md) and the remaining [feature guides](../../guides/README.md).

[Archive home](../README.md) · [Documentation home](../../README.md)

## Documents

| File | Subject | Why archived |
| --- | --- | --- |
| [DATASETS_GUIDE.md](DATASETS_GUIDE.md) | Dataset integration guide for the RAG system | Cites `scripts/convert_datasets.py`, `scripts/evaluate_datasets.py` and `scripts/upload_datasets.py`, none of which exist |
| [DATASET_USAGE_GUIDE.md](DATASET_USAGE_GUIDE.md) | Dataset usage guide for the RAG system | References Qdrant, which is removed; retrieval is PostgreSQL full-text |
| [GPT4o_MINI_SETUP_COMPLETE.md](GPT4o_MINI_SETUP_COMPLETE.md) | GPT-4o-mini Azure OpenAI setup completion note | One-off setup status note for a past provider configuration, not a procedure |
| [IMPLEMENTATION_GUIDE.md](IMPLEMENTATION_GUIDE.md) | Production-ready multimodal RAG implementation guide | References Qdrant and a pre-LangGraph architecture |
| [IMPLEMENTATION_GUIDE_REALTIME_PROCESSING.md](IMPLEMENTATION_GUIDE_REALTIME_PROCESSING.md) | Real-time document processing dashboard implementation guide | Describes an early WebSocket and Zustand design that was superseded |
| [QUICK_START.md](QUICK_START.md) | Quick start for the production-ready multimodal RAG system | Status claims and links (`deployment/PRODUCTION_DEPLOYMENT_GUIDE.md`, `deployment/RUNBOOKS.md`) no longer resolve |
| [REALTIME_PROCESSING_GUIDE.md](REALTIME_PROCESSING_GUIDE.md) | Real-time document processing guide | 12 of 13 cited backend and frontend paths do not exist |
| [REALTIME_QUICK_START.md](REALTIME_QUICK_START.md) | Real-time document processing quick start | Cites `frontend/react-components.md`, which does not exist |
| [SCALABILITY_AND_TESTING_GUIDE.md](SCALABILITY_AND_TESTING_GUIDE.md) | Scalability and testing implementation guide | References Qdrant and 11 test files and templates that do not exist |
| [uploaded_documents_extraction.md](uploaded_documents_extraction.md) | Extraction of nine uploaded documents from a database snapshot | One-off data dump from 2025-12-18 listing user and organization ids, file hashes and chat-log previews; not a guide |
| [document_upload_implementation_summary.md](document_upload_implementation_summary.md) | Document upload with automatic knowledge graph population summary | References Qdrant and four modules that do not exist |
| [mui-v7-migration-guide.md](mui-v7-migration-guide.md) | MUI v7 migration guide | The frontend no longer depends on `@mui/*`; it uses shadcn/ui and Radix |
