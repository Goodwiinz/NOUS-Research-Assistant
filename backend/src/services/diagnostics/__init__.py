"""
Retrieval diagnostics package for RAG pipeline instrumentation.

Captures per-query diagnostic traces across the hybrid search pipeline
(source searches, fusion, reranking, context truncation) and provides
bottleneck analysis.
"""

from .bottleneck_analyzer import BottleneckAnalyzer, BottleneckReport, Finding
from .diagnostics_store import DiagnosticsStore, DiagnosticsUnavailable
from .retrieval_diagnostics import (
    ContextDiagnostics,
    FusionDiagnostics,
    RerankDiagnostics,
    RetrievalTrace,
    SourceDiagnostics,
)

__all__ = [
    "SourceDiagnostics",
    "FusionDiagnostics",
    "RerankDiagnostics",
    "ContextDiagnostics",
    "RetrievalTrace",
    "DiagnosticsStore",
    "DiagnosticsUnavailable",
    "BottleneckAnalyzer",
    "BottleneckReport",
    "Finding",
]
