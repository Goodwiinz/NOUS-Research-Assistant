"""Neo4j credential resolution for ingestion paths.

Ingestion code must never authenticate with a literal fallback password.
NEO4J_PASSWORD is required at call time; URI and username keep their
non-secret environment defaults.
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Neo4jAuth:
    """Resolved Neo4j connection credentials."""

    uri: str
    user: str
    password: str


def get_neo4j_auth() -> Neo4jAuth:
    """Resolve Neo4j credentials from the environment.

    Raises RuntimeError when NEO4J_PASSWORD is unset or empty: ingestion
    paths fail loud rather than silently authenticating with a known literal.

    settings.NEO4J_PASSWORD is deliberately not used here: its validator
    substitutes "neo4jpassword" for local development, which would reintroduce
    a literal fallback on exactly the paths this helper protects.
    """
    password = os.getenv("NEO4J_PASSWORD")
    if not password:
        raise RuntimeError("NEO4J_PASSWORD must be set for Neo4j ingestion")
    return Neo4jAuth(
        uri=os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_USER", "neo4j"),
        password=password,
    )
