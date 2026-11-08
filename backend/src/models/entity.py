"""
Entity model for knowledge graph and extracted entities
"""

import uuid
from datetime import datetime
from enum import Enum as PyEnum
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    func,
    text,
)
from sqlalchemy.orm import relationship

from .base import GUID, BaseModel
from .utils import StringArray

# Association table for entity relationships
entity_relationships = Table(
    "entity_relationships",
    BaseModel.metadata,
    Column(
        "source_entity_id",
        GUID(),
        ForeignKey("entities.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "target_entity_id",
        GUID(),
        ForeignKey("entities.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("relationship_type", String(100), nullable=False),
    Column("confidence", Float, default=1.0, nullable=False),
    Column("relationship_metadata", JSON, nullable=True),
    Column(
        "created_at", DateTime(timezone=True), server_default=func.now(), nullable=False
    ),
)


class EntityType(PyEnum):
    """Entity types for different kinds of extracted entities"""

    PERSON = "person"
    ORGANIZATION = "organization"
    LOCATION = "location"
    PRODUCT = "product"
    CONCEPT = "concept"
    DATE = "date"
    NUMBER = "number"
    EMAIL = "email"
    PHONE = "phone"
    URL = "url"
    EVENT = "event"
    FINANCIAL = "financial"
    JOB_TITLE = "job_title"
    TOPIC = "topic"
    TECHNOLOGY = "technology"
    RESEARCH = "research"
    DOCUMENT = "document"
    CUSTOM = "custom"


class ExtractionMethod(PyEnum):
    """Methods used for entity extraction"""

    SPACY = "spacy"
    OPENAI = "openai"
    REGEX = "regex"
    MANUAL = "manual"
    GRAPH_EXTRACTION = "graph_extraction"


class Entity(BaseModel):
    """Entity model for extracted entities and knowledge graph"""

    __tablename__ = "entities"

    # Basic information
    entity_type = Column(Enum(EntityType), nullable=False, index=True)
    name = Column(String(500), nullable=False, index=True)
    canonical_name = Column(String(500), nullable=True, index=True)  # Standardized name
    aliases = Column(StringArray, nullable=True)  # Alternative names

    # Content and context
    description = Column(Text, nullable=True)
    properties = Column(JSON, nullable=True)  # Entity-specific properties
    confidence = Column(Float, default=1.0, nullable=False)
    relevance_score = Column(Float, default=1.0, nullable=False)

    # Extraction information
    extraction_method = Column(Enum(ExtractionMethod), nullable=False)
    extracted_at = Column(DateTime(timezone=True), nullable=False)
    extraction_model = Column(String(100), nullable=True)

    # Graph information
    graph_id = Column(String(255), nullable=True, index=True)  # Neo4j node ID
    is_in_knowledge_graph = Column(Boolean, default=False, nullable=False)

    # Relationships
    document_id = Column(
        GUID(), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    organization_id = Column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )

    # Database indexes for performance optimization
    __table_args__ = (Index("idx_entity_org_name", "organization_id", "name"),)

    # Relationships
    document = relationship("Document", back_populates="entities")
    organization = relationship("Organization")

    # Self-referential relationships (viewonly due to association table having extra columns)
    related_entities = relationship(
        "Entity",
        secondary=entity_relationships,
        primaryjoin="Entity.id == entity_relationships.c.source_entity_id",
        secondaryjoin="Entity.id == entity_relationships.c.target_entity_id",
        backref="related_by",
    )

    def __repr__(self):
        return f"<Entity(name={self.name}, type={self.entity_type.value}, confidence={self.confidence})>"

    @property
    def display_name(self) -> str:
        """Get display name (canonical name or original name)"""
        return self.canonical_name or self.name

    @property
    def all_names(self) -> list:
        """Get all names for the entity"""
        names = [self.name]
        if self.canonical_name and self.canonical_name != self.name:
            names.append(self.canonical_name)
        if self.aliases:
            names.extend(self.aliases)
        return list(set(names))  # Remove duplicates

    def add_alias(self, alias: str):
        """Add an alias for the entity"""
        if not self.aliases:
            self.aliases = []
        if (
            alias not in self.aliases
            and alias != self.name
            and alias != self.canonical_name
        ):
            self.aliases.append(alias)

    def remove_alias(self, alias: str):
        """Remove an alias from the entity"""
        if self.aliases and alias in self.aliases:
            self.aliases.remove(alias)

    def add_property(self, key: str, value):
        """Add a property to the entity"""
        if not self.properties:
            self.properties = {}
        self.properties[key] = value

    def get_property(self, key: str, default=None):
        """Get a property value"""
        if not self.properties:
            return default
        return self.properties.get(key, default)

    def add_relationship(
        self,
        target_entity,
        relationship_type: str,
        confidence: float = 1.0,
        metadata: dict = None,
    ):
        """Add a relationship to another entity"""
        if target_entity not in self.related_entities:
            self.related_entities.append(target_entity)
            # Add relationship metadata
            if not metadata:
                metadata = {}
            metadata["relationship_type"] = relationship_type
            metadata["confidence"] = confidence
            # Note: In a real implementation, you'd store this in the association table

    def remove_relationship(self, target_entity):
        """Remove a relationship to another entity"""
        if target_entity in self.related_entities:
            self.related_entities.remove(target_entity)

    def get_relationships_by_type(self, relationship_type: str) -> list:
        """Get related entities by relationship type"""
        # Note: This would need to query the association table in a real implementation
        return [entity for entity in self.related_entities]

    def update_confidence(self, new_confidence: float):
        """Update entity confidence"""
        if 0.0 <= new_confidence <= 1.0:
            self.confidence = new_confidence

    def update_relevance_score(self, new_score: float):
        """Update relevance score"""
        if 0.0 <= new_score <= 1.0:
            self.relevance_score = new_score

    def merge_with(self, other_entity):
        """Merge this entity with another entity"""
        if self.entity_type != other_entity.entity_type:
            raise ValueError("Cannot merge entities of different types")

        # Keep the higher confidence
        if other_entity.confidence > self.confidence:
            self.confidence = other_entity.confidence

        # Merge aliases
        if other_entity.aliases:
            if not self.aliases:
                self.aliases = []
            for alias in other_entity.aliases:
                self.add_alias(alias)

        # Merge properties
        if other_entity.properties:
            if not self.properties:
                self.properties = {}
            self.properties.update(other_entity.properties)

        # Use canonical name if other entity has one and this one doesn't
        if not self.canonical_name and other_entity.canonical_name:
            self.canonical_name = other_entity.canonical_name

        # Mark other entity as deleted
        other_entity.soft_delete()

    def to_dict(self, include_relationships: bool = False) -> dict:
        """Convert to dictionary"""
        data = super().to_dict()

        # Convert enum values
        data["entity_type"] = self.entity_type.value if self.entity_type else None
        data["extraction_method"] = (
            self.extraction_method.value if self.extraction_method else None
        )

        # Add computed fields
        data["display_name"] = self.display_name
        data["all_names"] = self.all_names

        # Include relationships if requested
        if include_relationships:
            data["related_entities"] = [
                {
                    "id": str(entity.id),
                    "name": entity.display_name,
                    "type": entity.entity_type.value,
                }
                for entity in self.related_entities
            ]

        return data

    @classmethod
    def get_entities_by_type(
        cls, entity_type: EntityType, organization_id: Optional[uuid.UUID] = None
    ) -> list:
        """Get entities by type"""
        query = cls.query.filter(
            cls.entity_type == entity_type, cls.is_deleted == False
        )
        if organization_id:
            query = query.filter(cls.organization_id == organization_id)
        return query.all()

    @classmethod
    def search_entities(
        cls,
        query_text: str,
        entity_type: EntityType = None,
        organization_id: Optional[uuid.UUID] = None,
    ) -> list:
        """Search entities by name or aliases"""
        query = cls.query.filter(
            cls.is_deleted == False,
            (
                cls.name.ilike(f"%{query_text}%")
                | cls.canonical_name.ilike(f"%{query_text}%")
            ),
        )

        if entity_type:
            query = query.filter(cls.entity_type == entity_type)

        if organization_id:
            query = query.filter(cls.organization_id == organization_id)

        return query.all()

    @classmethod
    def get_high_confidence_entities(
        cls, min_confidence: float = 0.8, organization_id: Optional[uuid.UUID] = None
    ) -> list:
        """Get high confidence entities"""
        query = cls.query.filter(
            cls.confidence >= min_confidence, cls.is_deleted == False
        )
        if organization_id:
            query = query.filter(cls.organization_id == organization_id)
        return query.all()
