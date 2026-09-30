"""Research Engine API routers."""

from .blueprints import router as research_engine_blueprints_router
from .capabilities import router as research_engine_capabilities_router
from .corpus import router as research_engine_corpus_router
from .identities import router as research_engine_identities_router
from .projects import router as research_engine_projects_router
from .protocols import router as research_engine_protocols_router
from .reviews import router as research_engine_reviews_router
from .runs import router as research_engine_runs_router
from .screening import router as research_engine_screening_router
from .steps import router as research_engine_steps_router

__all__ = [
    "research_engine_projects_router",
    "research_engine_protocols_router",
    "research_engine_blueprints_router",
    "research_engine_capabilities_router",
    "research_engine_runs_router",
    "research_engine_reviews_router",
    "research_engine_identities_router",
    "research_engine_screening_router",
    "research_engine_corpus_router",
    "research_engine_steps_router",
]
