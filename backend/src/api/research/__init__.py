"""
Research API routes for citations, projects, drafts, chat, export, project-chat integration,
tone engine, extraction matrix, AI writer, and research pipeline
"""

from .chat import router as chat_router
from .citations import router as citations_router
from .claims import router as claims_router
from .drafts import router as drafts_router
from .export import router as export_router
from .extraction_matrix import router as extraction_matrix_router
from .manuscript_releases import router as manuscript_releases_router
from .peer_review import router as peer_review_router
from .pipeline import router as pipeline_router
from .project_chat import router as project_chat_router
from .project_report import router as project_report_router
from .project_skills import router as project_skills_router
from .projects import router as projects_router
from .tone_engine import router as tone_engine_router
from .writer import router as writer_router

__all__ = [
    "citations_router",
    "projects_router",
    "project_report_router",
    "project_skills_router",
    "drafts_router",
    "claims_router",
    "peer_review_router",
    "manuscript_releases_router",
    "chat_router",
    "export_router",
    "project_chat_router",
    "tone_engine_router",
    "extraction_matrix_router",
    "writer_router",
    "pipeline_router",
]
