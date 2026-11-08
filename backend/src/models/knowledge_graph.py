"""
Knowledge Graph models for entity management and relationship tracking
"""

import uuid
from datetime import datetime
from datetime import timezone as dt_timezone
from enum import Enum as PyEnum
from typing import Any, Dict, List, Optional, Set, Union

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
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import relationship

from .base import GUID, BaseModel


class EntityType(PyEnum):
    """Knowledge graph entity types"""

    PERSON = "person"
    ORGANIZATION = "organization"
    LOCATION = "location"
    PRODUCT = "product"
    SERVICE = "service"
    TECHNOLOGY = "technology"
    CONCEPT = "concept"
    EVENT = "event"
    DATE = "date"
    NUMBER = "number"
    DOCUMENT = "document"
    PROJECT = "project"
    ROLE = "role"
    SKILL = "skill"
    INDUSTRY = "industry"
    MARKET = "market"
    COMPETITOR = "competitor"
    CUSTOMER = "customer"
    PARTNER = "partner"
    SUPPLIER = "supplier"
    REGULATION = "regulation"
    POLICY = "policy"
    STANDARD = "standard"
    CUSTOM = "custom"


class RelationshipType(PyEnum):
    """Knowledge graph relationship types"""

    # General relationships
    RELATED_TO = "related_to"
    PART_OF = "part_of"
    INSTANCE_OF = "instance_of"
    SUBCLASS_OF = "subclass_of"
    HAS_PROPERTY = "has_property"
    LOCATED_IN = "located_in"
    CREATED_AT = "created_at"
    WORKS_FOR = "works_for"
    MEMBER_OF = "member_of"
    KNOWS = "knows"
    COLLABORATES_WITH = "collaborates_with"

    # Business relationships
    CUSTOMER_OF = "customer_of"
    SUPPLIER_TO = "supplier_to"
    PARTNER_OF = "partner_of"
    COMPETITOR_OF = "competitor_of"
    SUBSIDIARY_OF = "subsidiary_of"
    ACQUIRES = "acquires"
    MERGES_WITH = "merges_with"
    INVESTS_IN = "invests_in"

    # Temporal relationships
    BEFORE = "before"
    AFTER = "after"
    DURING = "during"
    OVERLAPS_WITH = "overlaps_with"

    # Content relationships
    MENTIONS = "mentions"
    REFERENCES = "references"
    CITES = "cites"
    QUOTES = "quotes"
    DESCRIBES = "describes"
    DEFINES = "defines"
    EXAMPLE_OF = "example_of"

    # Technical relationships
    USES = "uses"
    IMPLEMENTS = "implements"
    DEPENDS_ON = "depends_on"
    REQUIRES = "requires"
    ENABLES = "enables"
    INTEGRATES_WITH = "integrates_with"
    INTERFACES_WITH = "interfaces_with"


class EntitySource(PyEnum):
    """Entity extraction sources"""

    MANUAL = "manual"
    SPACY = "spacy"
    OPENAI = "openai"
    HUGGINGFACE = "huggingface"
    REGEX = "regex"
    DICTIONARY = "dictionary"
    ONTOLOGY = "ontology"
    WIKIDATA = "wikidata"
    KNOWLEDGE_GRAPH = "knowledge_graph"
    CUSTOM_EXTRACTOR = "custom_extractor"


class ConfidenceLevel(PyEnum):
    """Confidence levels for entities and relationships"""

    VERY_LOW = "very_low"  # 0.0 - 0.2
    LOW = "low"  # 0.2 - 0.4
    MEDIUM = "medium"  # 0.4 - 0.6
    HIGH = "high"  # 0.6 - 0.8
    VERY_HIGH = "very_high"  # 0.8 - 1.0


class KnowledgeEntity(BaseModel):
    """Knowledge graph entity with comprehensive metadata"""

    __tablename__ = "knowledge_entities"

    # Basic entity information
    name = Column(String(500), nullable=False, index=True)
    canonical_name = Column(String(500), nullable=True, index=True)  # Standardized name
    entity_type = Column(Enum(EntityType), nullable=False, index=True)
    subtypes = Column(ARRAY(String), nullable=True)  # More specific types

    # Entity identity
    entity_uri = Column(
        String(1000), nullable=True, unique=True, index=True
    )  # Unique identifier
    external_ids = Column(
        JSON, nullable=True
    )  # IDs in external systems (Wikidata, etc.)
    aliases = Column(ARRAY(String), nullable=True)  # Alternative names
    abbreviations = Column(ARRAY(String), nullable=True)  # Abbreviations

    # Entity description and content
    description = Column(Text, nullable=True)
    summary = Column(Text, nullable=True)
    long_description = Column(Text, nullable=True)
    key_attributes = Column(JSON, nullable=True)  # Type-specific attributes

    # Extraction and source information
    source = Column(Enum(EntitySource), nullable=False, index=True)
    extraction_model = Column(String(100), nullable=True)
    extraction_confidence = Column(Float, nullable=False, index=True)
    confidence_level = Column(Enum(ConfidenceLevel), nullable=True, index=True)
    extracted_at = Column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )

    # Temporal information
    valid_from = Column(DateTime(timezone=True), nullable=True)
    valid_to = Column(DateTime(timezone=True), nullable=True)
    temporal_context = Column(JSON, nullable=True)  # Time-specific information

    # Geospatial information
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    address = Column(Text, nullable=True)
    country = Column(String(100), nullable=True)
    region = Column(String(100), nullable=True)
    city = Column(String(100), nullable=True)

    # Organization and ownership
    organization_id = Column(
        GUID(), ForeignKey("organizations.id"), nullable=False, index=True
    )
    owner_user_id = Column(GUID(), ForeignKey("users.id"), nullable=True)
    curator_user_id = Column(
        GUID(), ForeignKey("users.id"), nullable=True
    )  # Entity curator/manager

    # Validation and verification
    is_verified = Column(Boolean, default=False, nullable=False)
    verified_by_user_id = Column(GUID(), ForeignKey("users.id"), nullable=True)
    verified_at = Column(DateTime(timezone=True), nullable=True)
    verification_notes = Column(Text, nullable=True)

    # Quality metrics
    quality_score = Column(Float, nullable=True, index=True)
    completeness_score = Column(Float, nullable=True)
    accuracy_score = Column(Float, nullable=True)
    freshness_score = Column(Float, nullable=True)

    # Usage analytics
    mention_count = Column(Integer, default=0, nullable=False)
    document_count = Column(Integer, default=0, nullable=False)
    query_count = Column(Integer, default=0, nullable=False)
    last_mentioned_at = Column(DateTime(timezone=True), nullable=True)
    popularity_score = Column(Float, nullable=True)

    # Graph information
    degree_centrality = Column(Float, nullable=True)  # Number of connections
    betweenness_centrality = Column(Float, nullable=True)  # Importance in network
    pagerank_score = Column(Float, nullable=True)  # PageRank importance
    community_id = Column(String(100), nullable=True)  # Community cluster assignment

    # Integration with external systems
    neo4j_node_id = Column(String(255), nullable=True, index=True)
    vector_id = Column(
        String(255), nullable=True
    )  # Vector representation for similarity
    embedding_model = Column(String(100), nullable=True)

    # Entity lifecycle
    status = Column(
        String(20), nullable=False, default="active"
    )  # active, inactive, deprecated, merged
    merged_into_id = Column(GUID(), ForeignKey("knowledge_entities.id"), nullable=True)
    deprecated_at = Column(DateTime(timezone=True), nullable=True)
    deprecation_reason = Column(Text, nullable=True)

    # Custom attributes and metadata
    custom_attributes = Column(JSON, nullable=True)
    tags = Column(ARRAY(String), nullable=True)
    categories = Column(ARRAY(String), nullable=True)

    # Relationships
    organization = relationship("Organization", back_populates="knowledge_entities")
    owner_user = relationship("User", foreign_keys=[owner_user_id])
    curator_user = relationship("User", foreign_keys=[curator_user_id])
    verified_by_user = relationship("User", foreign_keys=[verified_by_user_id])
    merged_into = relationship(
        "KnowledgeEntity", remote_side=[id], backref="merged_from"
    )

    # Graph relationships
    outgoing_relationships = relationship(
        "EntityRelationship",
        foreign_keys="EntityRelationship.source_entity_id",
        back_populates="source_entity",
        cascade="all, delete-orphan",
    )
    incoming_relationships = relationship(
        "EntityRelationship",
        foreign_keys="EntityRelationship.target_entity_id",
        back_populates="target_entity",
        cascade="all, delete-orphan",
    )

    # Content relationships
    document_mentions = relationship(
        "EntityDocumentMention", back_populates="entity", cascade="all, delete-orphan"
    )
    entity_validations = relationship(
        "EntityValidation", back_populates="entity", cascade="all, delete-orphan"
    )

    # Indexes for performance
    __table_args__ = (
        Index("idx_entities_org_type", "organization_id", "entity_type"),
        Index("idx_entities_name_canonical", "name", "canonical_name"),
        Index("idx_entities_confidence", "extraction_confidence"),
        Index("idx_entities_quality", "quality_score"),
        Index("idx_entities_status", "status"),
        Index("idx_entities_neo4j", "neo4j_node_id"),
        Index("idx_entities_coordinates", "latitude", "longitude"),
        Index("idx_entities_temporal", "valid_from", "valid_to"),
    )

    def __repr__(self):
        return f"<KnowledgeEntity(name={self.name}, type={self.entity_type.value}, confidence={self.extraction_confidence})>"

    @property
    def all_names(self) -> Set[str]:
        """Get all names for the entity"""
        names = {self.name}
        if self.canonical_name:
            names.add(self.canonical_name)
        if self.aliases:
            names.update(self.aliases)
        if self.abbreviations:
            names.update(self.abbreviations)
        return names

    @property
    def display_name(self) -> str:
        """Get best display name for entity"""
        return self.canonical_name or self.name

    @property
    def is_active(self) -> bool:
        """Check if entity is active"""
        return self.status == "active" and not self.is_deleted

    @property
    def confidence_description(self) -> str:
        """Get confidence level description"""
        if self.confidence_level:
            return self.confidence_level.value.replace("_", " ").title()

        if self.extraction_confidence >= 0.8:
            return "Very High"
        elif self.extraction_confidence >= 0.6:
            return "High"
        elif self.extraction_confidence >= 0.4:
            return "Medium"
        elif self.extraction_confidence >= 0.2:
            return "Low"
        else:
            return "Very Low"

    def add_alias(self, alias: str):
        """Add an alias for the entity"""
        if not self.aliases:
            self.aliases = []
        if alias not in self.aliases and alias not in self.all_names:
            self.aliases.append(alias)

    def remove_alias(self, alias: str):
        """Remove an alias from the entity"""
        if self.aliases and alias in self.aliases:
            self.aliases.remove(alias)

    def add_external_id(self, system: str, external_id: str):
        """Add external system ID"""
        if not self.external_ids:
            self.external_ids = {}
        self.external_ids[system] = external_id

    def get_external_id(self, system: str) -> Optional[str]:
        """Get external system ID"""
        if not self.external_ids:
            return None
        return self.external_ids.get(system)

    def set_attribute(self, key: str, value: Any):
        """Set a custom attribute"""
        if not self.custom_attributes:
            self.custom_attributes = {}
        self.custom_attributes[key] = value

    def get_attribute(self, key: str, default: Any = None) -> Any:
        """Get a custom attribute"""
        if not self.custom_attributes:
            return default
        return self.custom_attributes.get(key, default)

    def update_quality_scores(
        self,
        completeness: float = None,
        accuracy: float = None,
        freshness: float = None,
    ):
        """Update quality scores"""
        if completeness is not None:
            self.completeness_score = completeness
        if accuracy is not None:
            self.accuracy_score = accuracy
        if freshness is not None:
            self.freshness_score = freshness

        # Calculate overall quality score
        scores = [
            self.completeness_score or 0.5,
            self.accuracy_score or 0.5,
            self.freshness_score or 0.5,
            self.extraction_confidence,
        ]
        self.quality_score = sum(scores) / len(scores)

    def verify_entity(self, verified_by: uuid.UUID, notes: str = None):
        """Mark entity as verified"""
        self.is_verified = True
        self.verified_by_user_id = verified_by
        self.verified_at = datetime.utcnow()
        if notes:
            self.verification_notes = notes

    def merge_with(self, target_entity_id: uuid.UUID, merge_reason: str = None):
        """Merge this entity into another entity"""
        self.status = "merged"
        self.merged_into_id = target_entity_id
        self.deprecated_at = datetime.utcnow()
        if merge_reason:
            self.deprecation_reason = merge_reason

    def add_mention(
        self, document_id: uuid.UUID, context: str = None, confidence: float = None
    ):
        """Add a document mention (creates mention record)"""
        mention = EntityDocumentMention(
            entity_id=self.id,
            document_id=document_id,
            mention_context=context,
            confidence=confidence or self.extraction_confidence,
        )
        self.mention_count += 1
        self.last_mentioned_at = datetime.utcnow()
        return mention

    def calculate_centrality_metrics(self, relationship_count: int):
        """Update centrality metrics based on relationships"""
        self.degree_centrality = float(relationship_count)
        # Note: betweenness_centrality and pagerank_score would require graph analysis

    def to_dict(
        self, include_relationships: bool = False, include_attributes: bool = True
    ) -> Dict[str, Any]:
        """Convert to dictionary"""
        data = super().to_dict()

        # Convert enum values
        data.update(
            {
                "entity_type": self.entity_type.value if self.entity_type else None,
                "source": self.source.value if self.source else None,
                "confidence_level": (
                    self.confidence_level.value if self.confidence_level else None
                ),
            }
        )

        # Add computed properties
        data.update(
            {
                "all_names": list(self.all_names),
                "display_name": self.display_name,
                "is_active": self.is_active,
                "confidence_description": self.confidence_description,
            }
        )

        # Include/exclude data based on parameters
        if not include_attributes:
            data.pop("custom_attributes", None)
            data.pop("key_attributes", None)

        if include_relationships:
            data["relationships"] = {
                "outgoing": [rel.to_dict() for rel in self.outgoing_relationships],
                "incoming": [rel.to_dict() for rel in self.incoming_relationships],
            }

        return data

    @classmethod
    def search_entities(
        cls,
        search_term: str,
        organization_id: Optional[uuid.UUID] = None,
        entity_types: Optional[List[EntityType]] = None,
        limit: int = 50,
    ) -> List:
        """Search entities by name, aliases, or description"""
        from sqlalchemy import or_

        query = cls.query.filter(
            or_(
                cls.name.ilike(f"%{search_term}%"),
                cls.canonical_name.ilike(f"%{search_term}%"),
                cls.description.ilike(f"%{search_term}%"),
            ),
            cls.is_deleted == False,
        )

        if organization_id:
            query = query.filter(cls.organization_id == organization_id)

        if entity_types:
            query = query.filter(cls.entity_type.in_(entity_types))

        return query.limit(limit).all()

    @classmethod
    def get_entities_by_type(
        cls, entity_type: EntityType, organization_id: Optional[uuid.UUID] = None
    ) -> List:
        """Get entities by type"""
        query = cls.query.filter(
            cls.entity_type == entity_type, cls.is_deleted == False
        )

        if organization_id:
            query = query.filter(cls.organization_id == organization_id)

        return query.all()

    @classmethod
    def get_popular_entities(
        cls, organization_id: Optional[uuid.UUID] = None, limit: int = 100
    ) -> List:
        """Get most popular entities by mention count"""
        query = cls.query.filter(cls.is_deleted == False).order_by(
            cls.mention_count.desc()
        )

        if organization_id:
            query = query.filter(cls.organization_id == organization_id)

        return query.limit(limit).all()


class EntityRelationship(BaseModel):
    """Relationships between knowledge entities"""

    __tablename__ = "entity_relationships"

    # Relationship endpoints
    source_entity_id = Column(
        GUID(), ForeignKey("knowledge_entities.id"), nullable=False, index=True
    )
    target_entity_id = Column(
        GUID(), ForeignKey("knowledge_entities.id"), nullable=False, index=True
    )
    relationship_type = Column(Enum(RelationshipType), nullable=False, index=True)

    # Relationship direction and properties
    is_bidirectional = Column(Boolean, default=False, nullable=False)
    inverse_relationship_type = Column(
        Enum(RelationshipType), nullable=True
    )  # For bidirectional relationships

    # Relationship attributes
    properties = Column(JSON, nullable=True)  # Relationship-specific attributes
    weight = Column(Float, default=1.0, nullable=False)  # Relationship strength
    confidence = Column(Float, nullable=False, index=True)
    confidence_level = Column(Enum(ConfidenceLevel), nullable=True)

    # Temporal information
    valid_from = Column(DateTime(timezone=True), nullable=True)
    valid_to = Column(DateTime(timezone=True), nullable=True)
    relationship_date = Column(
        DateTime(timezone=True), nullable=True
    )  # When the relationship occurred

    # Source and extraction information
    source = Column(Enum(EntitySource), nullable=False)
    extraction_model = Column(String(100), nullable=True)
    extracted_at = Column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )

    # Context and evidence
    source_documents = Column(
        ARRAY(GUID()), nullable=True
    )  # Documents that support this relationship
    evidence_text = Column(Text, nullable=True)  # Text evidence for relationship
    context = Column(JSON, nullable=True)  # Additional context

    # Validation and verification
    is_verified = Column(Boolean, default=False, nullable=False)
    verified_by_user_id = Column(GUID(), ForeignKey("users.id"), nullable=True)
    verified_at = Column(DateTime(timezone=True), nullable=True)
    verification_notes = Column(Text, nullable=True)

    # Usage metrics
    mention_count = Column(Integer, default=0, nullable=False)
    query_count = Column(Integer, default=0, nullable=False)
    last_used_at = Column(DateTime(timezone=True), nullable=True)

    # Integration with graph systems
    neo4j_relationship_id = Column(String(255), nullable=True)

    # Relationships
    source_entity = relationship(
        "KnowledgeEntity",
        foreign_keys=[source_entity_id],
        back_populates="outgoing_relationships",
    )
    target_entity = relationship(
        "KnowledgeEntity",
        foreign_keys=[target_entity_id],
        back_populates="incoming_relationships",
    )
    verified_by_user = relationship("User", foreign_keys=[verified_by_user_id])

    # Indexes for performance
    __table_args__ = (
        Index("idx_relationships_source_type", "source_entity_id", "relationship_type"),
        Index("idx_relationships_target_type", "target_entity_id", "relationship_type"),
        Index("idx_relationships_confidence", "confidence"),
        Index("idx_relationships_weight", "weight"),
        Index("idx_relationships_temporal", "valid_from", "valid_to"),
        Index("idx_relationships_neo4j", "neo4j_relationship_id"),
    )

    def __repr__(self):
        return f"<EntityRelationship(source={self.source_entity_id}, type={self.relationship_type.value}, target={self.target_entity_id})>"

    @property
    def is_valid_now(self) -> bool:
        """Check if relationship is currently valid"""
        now = datetime.utcnow()
        if self.valid_from and now < self.valid_from:
            return False
        if self.valid_to and now > self.valid_to:
            return False
        return True

    def add_source_document(self, document_id: uuid.UUID):
        """Add a source document that supports this relationship"""
        if not self.source_documents:
            self.source_documents = []
        if document_id not in self.source_documents:
            self.source_documents.append(document_id)

    def verify_relationship(self, verified_by: uuid.UUID, notes: str = None):
        """Mark relationship as verified"""
        self.is_verified = True
        self.verified_by_user_id = verified_by
        self.verified_at = datetime.utcnow()
        if notes:
            self.verification_notes = notes

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        data = super().to_dict()

        # Convert enum values
        data.update(
            {
                "relationship_type": (
                    self.relationship_type.value if self.relationship_type else None
                ),
                "confidence_level": (
                    self.confidence_level.value if self.confidence_level else None
                ),
                "source": self.source.value if self.source else None,
                "inverse_relationship_type": (
                    self.inverse_relationship_type.value
                    if self.inverse_relationship_type
                    else None
                ),
            }
        )

        # Add computed properties
        data["is_valid_now"] = self.is_valid_now

        # Include related entity information
        if self.source_entity:
            data["source_entity"] = {
                "id": str(self.source_entity.id),
                "name": self.source_entity.display_name,
                "type": self.source_entity.entity_type.value,
            }
        if self.target_entity:
            data["target_entity"] = {
                "id": str(self.target_entity.id),
                "name": self.target_entity.display_name,
                "type": self.target_entity.entity_type.value,
            }

        return data


class EntityDocumentMention(BaseModel):
    """Mentions of entities in documents"""

    __tablename__ = "entity_document_mentions"

    entity_id = Column(
        GUID(), ForeignKey("knowledge_entities.id"), nullable=False, index=True
    )
    document_id = Column(
        GUID(), ForeignKey("enhanced_documents.id"), nullable=False, index=True
    )

    # Mention location and context
    mention_text = Column(Text, nullable=False)  # Exact text that mentions the entity
    mention_start = Column(Integer, nullable=False)  # Character position in document
    mention_end = Column(Integer, nullable=False)  # Character position in document
    mention_context = Column(Text, nullable=True)  # Surrounding context
    page_number = Column(Integer, nullable=True)  # Page number (for PDFs)
    section_title = Column(String(500), nullable=True)  # Section where mention occurs

    # Mention attributes
    confidence = Column(Float, nullable=False)
    is_coreference = Column(
        Boolean, default=False, nullable=False
    )  # Is this a coreference mention?
    mention_type = Column(
        String(50), nullable=True
    )  # exact, partial, abbreviation, etc.

    # Extraction information
    extracted_at = Column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )
    extraction_model = Column(String(100), nullable=True)

    # Relationships
    entity = relationship("KnowledgeEntity", back_populates="document_mentions")
    document = relationship("EnhancedDocument")

    # Indexes
    __table_args__ = (
        Index("idx_mentions_entity_document", "entity_id", "document_id"),
        Index("idx_mentions_document", "document_id"),
        Index("idx_mentions_confidence", "confidence"),
        Index("idx_mentions_location", "mention_start", "mention_end"),
    )

    def __repr__(self):
        return f"<EntityDocumentMention(entity={self.entity_id}, document={self.document_id}, text='{self.mention_text[:20]}...')>"

    @property
    def mention_length(self) -> int:
        """Get length of mention"""
        return self.mention_end - self.mention_start

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        data = super().to_dict()

        # Add computed properties
        data["mention_length"] = self.mention_length

        # Include related entity information
        if self.entity:
            data["entity"] = {
                "id": str(self.entity.id),
                "name": self.entity.display_name,
                "type": self.entity.entity_type.value,
            }

        return data


class EntityValidation(BaseModel):
    """Validation records for entities"""

    __tablename__ = "entity_validations"

    entity_id = Column(GUID(), ForeignKey("knowledge_entities.id"), nullable=False)

    # Validation details
    validation_type = Column(
        String(50), nullable=False
    )  # accuracy, completeness, consistency, etc.
    validation_status = Column(
        String(20), nullable=False
    )  # passed, failed, needs_review
    validation_score = Column(Float, nullable=True)

    # Validation criteria and results
    criteria_checked = Column(JSON, nullable=True)
    validation_results = Column(JSON, nullable=True)
    issues_found = Column(JSON, nullable=True)
    recommendations = Column(JSON, nullable=True)

    # Validation metadata
    validated_by_user_id = Column(GUID(), ForeignKey("users.id"), nullable=True)
    validated_at = Column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )
    validation_method = Column(
        String(50), nullable=True
    )  # manual, automated, peer_review
    comments = Column(Text, nullable=True)

    # Relationships
    entity = relationship("KnowledgeEntity", back_populates="entity_validations")
    validated_by_user = relationship("User", foreign_keys=[validated_by_user_id])


# Association table for many-to-many relationships between entities and categories
entity_categories = Table(
    "entity_categories",
    BaseModel.metadata,
    Column("entity_id", GUID(), ForeignKey("knowledge_entities.id"), primary_key=True),
    Column("category_id", GUID(), ForeignKey("categories.id"), primary_key=True),
    Column("confidence", Float, default=1.0, nullable=False),
    Column(
        "assigned_at",
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    ),
)

# Association table for entity synonyms and variations
entity_synonyms = Table(
    "entity_synonyms",
    BaseModel.metadata,
    Column("entity_id", GUID(), ForeignKey("knowledge_entities.id"), primary_key=True),
    Column("synonym_text", String(500), primary_key=True),
    Column(
        "synonym_type", String(50), nullable=False
    ),  # alias, abbreviation, translation, etc.
    Column("confidence", Float, default=1.0, nullable=False),
    Column(
        "created_at", DateTime(timezone=True), server_default=func.now(), nullable=False
    ),
)
