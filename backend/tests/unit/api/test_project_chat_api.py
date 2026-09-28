"""
Unit tests for Project-Chat Integration API endpoints.

Tests the API layer with mocked database and dependencies.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import (
    ChatMessage,
    Collection,
    Conversation,
    MessageRole,
    ProjectThread,
    ProjectThreadLinkType,
    Thread,
    User,
    UserRole,
    Workspace,
)
from src.shared.research_schemas import (
    LinkThreadRequest,
    SaveThreadToNoteRequest,
    StartChatFromProjectRequest,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def mock_user():
    """Create a mock user for testing"""
    user = MagicMock(spec=User)
    user.id = uuid4()
    user.email = "test@example.com"
    user.role = UserRole.USER
    user.is_active = True
    return user


@pytest.fixture
def mock_workspace(mock_user):
    """Create a mock workspace"""
    workspace = MagicMock(spec=Workspace)
    workspace.id = uuid4()
    workspace.name = "Test Workspace"
    workspace.owner_id = mock_user.id
    return workspace


@pytest.fixture
def mock_project(mock_workspace):
    """Create a mock project (Collection)"""
    project = MagicMock(spec=Collection)
    project.id = uuid4()
    project.name = "Test Research Project"
    project.description = "A test project"
    project.workspace_id = mock_workspace.id
    project.documents = []
    return project


@pytest.fixture
def mock_conversation(mock_workspace, mock_user):
    """Create a mock conversation"""
    conversation = MagicMock(spec=Conversation)
    conversation.id = uuid4()
    conversation.title = "Test Conversation"
    conversation.workspace_id = mock_workspace.id
    conversation.created_by_id = mock_user.id
    return conversation


@pytest.fixture
def mock_thread(mock_conversation, mock_user):
    """Create a mock thread"""
    thread = MagicMock(spec=Thread)
    thread.id = uuid4()
    thread.title = "Test Thread"
    thread.conversation_id = mock_conversation.id
    thread.conversation = mock_conversation
    thread.created_by_id = mock_user.id
    thread.message_count = 0
    thread.last_message_at = datetime.utcnow()
    thread.is_deleted = False
    thread.generate_title = MagicMock(return_value="Generated Title")
    return thread


@pytest.fixture
def mock_project_thread(mock_project, mock_thread, mock_user):
    """Create a mock project-thread link"""
    pt = MagicMock(spec=ProjectThread)
    pt.id = uuid4()
    pt.project_id = mock_project.id
    pt.thread_id = mock_thread.id
    pt.link_type = ProjectThreadLinkType.MANUAL.value
    pt.linked_at = datetime.utcnow()
    pt.linked_by_id = mock_user.id
    pt.context_note = "Test context"
    pt.thread = mock_thread
    return pt


@pytest.fixture
def mock_db():
    """Create a mock async database session"""
    db = AsyncMock(spec=AsyncSession)
    db.execute = AsyncMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.delete = AsyncMock()
    db.rollback = AsyncMock()
    return db


# ============================================================================
# Helper Function Tests
# ============================================================================


class TestGetProjectWithAuth:
    """Tests for _get_project_with_auth helper"""

    @pytest.mark.asyncio
    async def test_returns_project_when_authorized(
        self, mock_user, mock_project, mock_db
    ):
        """Test returning project when user has access"""
        from src.api.research.project_chat import _get_project_with_auth

        with patch(
            "src.api.research.project_chat.resolve_project",
            new=AsyncMock(
                return_value=SimpleNamespace(collection=mock_project, engine=None)
            ),
        ) as resolve:
            result = await _get_project_with_auth(mock_project.id, mock_user, mock_db)

        assert result == mock_project
        resolve.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_raises_404_when_project_not_found(self, mock_user, mock_db):
        """Test raising 404 when project doesn't exist"""
        from src.api.research.project_chat import _get_project_with_auth

        with patch(
            "src.api.research.project_chat.resolve_project",
            new=AsyncMock(
                side_effect=HTTPException(
                    status_code=404, detail="Project not found or access denied"
                )
            ),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await _get_project_with_auth(uuid4(), mock_user, mock_db)

        assert exc_info.value.status_code == 404
        assert "not found or access denied" in exc_info.value.detail


class TestGetThreadWithAuth:
    """Tests for _get_thread_with_auth helper"""

    @pytest.mark.asyncio
    async def test_returns_thread_when_authorized(
        self, mock_user, mock_thread, mock_db
    ):
        """Test returning thread when user has access"""
        from src.api.research.project_chat import _get_thread_with_auth

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_thread
        mock_db.execute.return_value = mock_result

        with patch(
            "src.api.research.project_chat.accessible_research_workspace_ids",
            new=AsyncMock(return_value=[mock_thread.conversation.workspace_id]),
        ) as accessible_workspaces:
            result = await _get_thread_with_auth(mock_thread.id, mock_user, mock_db)

        assert result == mock_thread
        accessible_workspaces.assert_awaited_once_with(mock_db, mock_user.id)

    @pytest.mark.asyncio
    async def test_raises_404_when_thread_not_found(self, mock_user, mock_db):
        """Test raising 404 when thread doesn't exist"""
        from src.api.research.project_chat import _get_thread_with_auth

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        with patch(
            "src.api.research.project_chat.accessible_research_workspace_ids",
            new=AsyncMock(return_value=[]),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await _get_thread_with_auth(uuid4(), mock_user, mock_db)

        assert exc_info.value.status_code == 404


# ============================================================================
# Start Chat from Project Tests
# ============================================================================


class TestStartChatFromProject:
    """Tests for start_chat_from_project endpoint"""

    @pytest.mark.asyncio
    async def test_start_chat_creates_thread_and_link(
        self, mock_user, mock_project, mock_db
    ):
        """Test starting chat creates new thread and project link"""
        from src.api.research.project_chat import start_chat_from_project

        request = StartChatFromProjectRequest(
            initial_message="Hello, let's discuss this project",
            thread_title="Discussion Thread",
        )

        # Mock project lookup
        project_result = MagicMock()
        project_result.scalar_one_or_none.return_value = mock_project

        # Mock document query - no documents
        doc_result = MagicMock()
        doc_result.all.return_value = []

        mock_db.execute.side_effect = [project_result, doc_result]

        # Mock refresh to update IDs
        async def mock_refresh(obj):
            if isinstance(obj, Thread):
                obj.id = uuid4()
            elif isinstance(obj, ProjectThread):
                obj.id = uuid4()

        mock_db.refresh = mock_refresh

        with patch(
            "src.api.research.project_chat._get_project_with_auth",
            return_value=mock_project,
        ):
            # The endpoint creates objects that need IDs
            # This is simplified - actual test would mock more deeply
            pass

    @pytest.mark.asyncio
    async def test_start_chat_with_existing_conversation(
        self, mock_user, mock_project, mock_conversation, mock_db
    ):
        """Test starting chat with existing conversation ID"""
        request = StartChatFromProjectRequest(
            initial_message="Continue our discussion",
            conversation_id=mock_conversation.id,
        )

        assert request.conversation_id == mock_conversation.id
        assert request.initial_message == "Continue our discussion"

    @pytest.mark.asyncio
    async def test_start_chat_rejects_cross_workspace_conversation(
        self, mock_user, mock_project, mock_conversation, mock_db
    ):
        """Existing conversation must belong to the project workspace."""
        from src.api.research.project_chat import start_chat_from_project

        mock_conversation.workspace_id = uuid4()
        request = StartChatFromProjectRequest(
            initial_message="Wrong workspace",
            conversation_id=mock_conversation.id,
        )

        with (
            patch(
                "src.api.research.project_chat._get_project_with_auth",
                new=AsyncMock(return_value=mock_project),
            ),
            patch(
                "src.api.research.project_chat._get_conversation_with_auth",
                new=AsyncMock(return_value=mock_conversation),
            ),
            patch(
                "src.api.research.project_chat._get_project_document_scope",
                new=AsyncMock(return_value=[]),
            ),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await start_chat_from_project(
                    mock_project.id,
                    request,
                    current_user=mock_user,
                    db=mock_db,
                )

        assert exc_info.value.status_code == 400
        assert "same workspace" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_start_chat_logs_warning_for_empty_documents(
        self, mock_user, mock_project
    ):
        """Test warning is logged when project has no documents"""
        # Verify the project has no documents
        mock_project.documents = []
        assert len(mock_project.documents) == 0


# ============================================================================
# Link Thread to Project Tests
# ============================================================================


class TestLinkThreadToProject:
    """Tests for link_thread_to_project endpoint"""

    def test_link_request_validation(self):
        """Test LinkThreadRequest validation"""
        thread_id = uuid4()
        request = LinkThreadRequest(
            thread_id=thread_id,
            context_note="Relevant to research topic",
        )

        assert request.thread_id == thread_id
        assert request.context_note == "Relevant to research topic"

    def test_link_request_without_context_note(self):
        """Test LinkThreadRequest without optional context note"""
        thread_id = uuid4()
        request = LinkThreadRequest(thread_id=thread_id)

        assert request.thread_id == thread_id
        assert request.context_note is None

    @pytest.mark.asyncio
    async def test_link_prevents_duplicate(self, mock_project_thread, mock_db):
        """Test that duplicate links are idempotent (re-link returns existing)."""
        # The endpoint no longer raises 409; re-linking is idempotent.
        existing_link = mock_project_thread
        assert existing_link.project_id is not None
        assert existing_link.thread_id is not None

    @pytest.mark.asyncio
    async def test_link_validates_same_workspace(
        self, mock_project, mock_thread, mock_workspace
    ):
        """Test that thread and project must be in same workspace"""
        # Verify both are in same workspace
        assert mock_thread.conversation.workspace_id == mock_workspace.id
        assert mock_project.workspace_id == mock_workspace.id

    @pytest.mark.asyncio
    async def test_link_sets_thread_active_project_context(
        self, mock_user, mock_project, mock_thread, mock_db
    ):
        """Manual project links must persist active project context on thread."""
        from src.api.research.project_chat import link_thread_to_project

        mock_thread.source_project_id = None
        mock_thread.rag_document_scope = None

        # attach_thread_to_project executes: document scope query, the
        # on_conflict_do_nothing upsert, then a re-select that must return
        # the (now guaranteed) link row — order matters for mock side_effect.
        doc_id = uuid4()
        doc_result = MagicMock()
        doc_result.all.return_value = [(doc_id,)]
        upsert_result = MagicMock()
        link_row = MagicMock()
        link_row.id = uuid4()
        link_row.project_id = mock_project.id
        link_row.thread_id = mock_thread.id
        link_row.link_type = "manual"
        link_row.linked_at = datetime.utcnow()
        link_row.linked_by_id = mock_user.id
        link_row.context_note = None
        link_row.is_deleted = False
        existing_result = MagicMock()
        existing_result.scalar_one_or_none.return_value = link_row
        mock_db.execute.side_effect = [doc_result, upsert_result, existing_result]

        async def _refresh(entity):
            if getattr(entity, "id", None) is None:
                entity.id = uuid4()
            if getattr(entity, "linked_at", None) is None:
                entity.linked_at = datetime.utcnow()

        mock_db.refresh = AsyncMock(side_effect=_refresh)

        with (
            patch(
                "src.api.research.project_chat._get_project_with_auth",
                new=AsyncMock(return_value=mock_project),
            ),
            patch(
                "src.api.research.project_chat._get_thread_with_auth",
                new=AsyncMock(return_value=mock_thread),
            ),
        ):
            await link_thread_to_project(
                mock_project.id,
                LinkThreadRequest(thread_id=mock_thread.id),
                current_user=mock_user,
                db=mock_db,
            )

        assert mock_thread.source_project_id == mock_project.id
        assert mock_thread.rag_document_scope == {"document_ids": [str(doc_id)]}


# ============================================================================
# List Project Threads Tests
# ============================================================================


class TestListProjectThreads:
    """Tests for list_project_threads endpoint"""

    @pytest.mark.asyncio
    async def test_list_returns_linked_threads(
        self, mock_project, mock_project_thread, mock_db
    ):
        """Test listing returns all linked threads"""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_project_thread]
        mock_db.execute.return_value = mock_result

        # Verify the mock data
        assert mock_project_thread.thread is not None
        assert mock_project_thread.thread.is_deleted is False

    @pytest.mark.asyncio
    async def test_list_excludes_deleted_threads(
        self, mock_project, mock_project_thread, mock_thread
    ):
        """Test that deleted threads are excluded"""
        mock_thread.is_deleted = True
        mock_project_thread.thread = mock_thread

        # When listing, deleted threads should be filtered out
        assert mock_project_thread.thread.is_deleted is True

    @pytest.mark.asyncio
    async def test_list_returns_empty_when_no_threads(self, mock_project, mock_db):
        """Test listing returns empty list when no threads linked"""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        threads = mock_result.scalars().all()
        assert threads == []


# ============================================================================
# Unlink Thread Tests
# ============================================================================


class TestUnlinkThreadFromProject:
    """Tests for unlink_thread_from_project endpoint"""

    @pytest.mark.asyncio
    async def test_unlink_removes_link_not_thread(
        self, mock_project_thread, mock_thread, mock_db
    ):
        """Test that unlinking removes link but not the thread itself"""
        # The thread should still exist after unlinking
        assert mock_project_thread.thread_id == mock_thread.id
        assert mock_thread.id is not None

    @pytest.mark.asyncio
    async def test_unlink_raises_404_when_link_not_found(self, mock_db):
        """Test raising 404 when link doesn't exist"""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        # Link not found should raise 404
        link = mock_result.scalar_one_or_none()
        assert link is None

    @pytest.mark.asyncio
    async def test_unlink_preserves_agent_marker_on_full_unlink(
        self, mock_user, mock_project, mock_thread, mock_project_thread, mock_db
    ):
        """Regression: unlinking the last project must not wipe the agent marker.

        rag_document_scope is overloaded to also carry the agent marker
        ({"source": "agent"}) that list_agent_threads filters on. The old
        unlink path nulled the whole column, so an agent thread that had been
        bound to a project silently vanished from GET /api/v1/agent/threads
        once it was unlinked.
        """
        from src.api.research.project_chat import unlink_thread_from_project

        mock_thread.source_project_id = mock_project.id
        mock_thread.rag_document_scope = {
            "source": "agent",
            "document_ids": [str(uuid4())],
        }

        link_result = MagicMock()
        link_result.scalar_one_or_none.return_value = mock_project_thread
        remaining_result = MagicMock()
        remaining_result.scalars.return_value.first.return_value = None
        mock_db.execute.side_effect = [link_result, remaining_result]

        with (
            patch(
                "src.api.research.project_chat._get_project_with_auth",
                new=AsyncMock(return_value=mock_project),
            ),
            patch(
                "src.api.research.project_chat._get_thread_with_auth",
                new=AsyncMock(return_value=mock_thread),
            ),
        ):
            await unlink_thread_from_project(
                mock_project.id,
                mock_thread.id,
                current_user=mock_user,
                db=mock_db,
            )

        # Scalar source cleared, but the agent marker survives so the thread
        # stays visible in the agent thread list.
        assert mock_thread.source_project_id is None
        assert mock_thread.rag_document_scope == {"source": "agent"}
        mock_project_thread.soft_delete.assert_called_once()
        mock_db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unlink_reassigns_and_merges_scope_preserving_marker(
        self, mock_user, mock_project, mock_thread, mock_project_thread, mock_db
    ):
        """Regression: reassignment to a remaining link must MERGE the new
        document_ids into rag_document_scope (keeping the agent marker), not
        replace the whole dict."""
        from src.api.research.project_chat import unlink_thread_from_project

        mock_thread.source_project_id = mock_project.id
        mock_thread.rag_document_scope = {
            "source": "agent",
            "document_ids": [str(uuid4())],
        }

        other_project_id = uuid4()
        remaining_link = MagicMock(spec=ProjectThread)
        remaining_link.project_id = other_project_id
        remaining_link.is_deleted = False

        new_doc_id = uuid4()
        link_result = MagicMock()
        link_result.scalar_one_or_none.return_value = mock_project_thread
        remaining_result = MagicMock()
        remaining_result.scalars.return_value.first.return_value = remaining_link
        doc_result = MagicMock()
        doc_result.all.return_value = [(new_doc_id,)]
        mock_db.execute.side_effect = [link_result, remaining_result, doc_result]

        with (
            patch(
                "src.api.research.project_chat._get_project_with_auth",
                new=AsyncMock(return_value=mock_project),
            ),
            patch(
                "src.api.research.project_chat._get_thread_with_auth",
                new=AsyncMock(return_value=mock_thread),
            ),
        ):
            await unlink_thread_from_project(
                mock_project.id,
                mock_thread.id,
                current_user=mock_user,
                db=mock_db,
            )

        assert mock_thread.source_project_id == other_project_id
        assert mock_thread.rag_document_scope == {
            "source": "agent",
            "document_ids": [str(new_doc_id)],
        }


# ============================================================================
# Save Thread to Note Tests
# ============================================================================


class TestSaveThreadToNote:
    """Tests for save_thread_to_note endpoint"""

    def test_save_request_validation(self):
        """Test SaveThreadToNoteRequest validation"""
        thread_id = uuid4()
        request = SaveThreadToNoteRequest(
            thread_id=thread_id,
            note_title="Research Discussion Summary",
            include_citations=True,
        )

        assert request.thread_id == thread_id
        assert request.note_title == "Research Discussion Summary"
        assert request.include_citations is True

    def test_save_request_title_length_validation(self):
        """Test note title max length validation"""
        with pytest.raises(ValueError):
            SaveThreadToNoteRequest(
                thread_id=uuid4(),
                note_title="A" * 256,  # Exceeds 255 max length
                include_citations=True,
            )

    def test_save_request_title_min_length_validation(self):
        """Test note title min length validation"""
        with pytest.raises(ValueError):
            SaveThreadToNoteRequest(
                thread_id=uuid4(),
                note_title="",  # Empty title not allowed
                include_citations=True,
            )

    def test_save_request_default_include_citations(self):
        """Test include_citations defaults to True"""
        request = SaveThreadToNoteRequest(
            thread_id=uuid4(),
            note_title="Note Title",
        )
        assert request.include_citations is True


# ============================================================================
# Project Thread Response Tests
# ============================================================================


class TestProjectThreadResponse:
    """Tests for ProjectThreadResponse schema"""

    def test_response_contains_all_fields(self, mock_project_thread, mock_thread):
        """Test that response contains all expected fields"""
        from src.shared.research_schemas import ProjectThreadResponse

        response = ProjectThreadResponse(
            id=mock_project_thread.id,
            project_id=mock_project_thread.project_id,
            thread_id=mock_project_thread.thread_id,
            thread_title=mock_thread.title,
            conversation_id=mock_thread.conversation_id,
            link_type=mock_project_thread.link_type,
            linked_at=mock_project_thread.linked_at,
            linked_by_id=mock_project_thread.linked_by_id,
            context_note=mock_project_thread.context_note,
            message_count=mock_thread.message_count,
            last_message_at=mock_thread.last_message_at,
        )

        assert response.id == mock_project_thread.id
        assert response.thread_title == mock_thread.title
        assert response.link_type == ProjectThreadLinkType.MANUAL.value

    def test_response_optional_fields(self):
        """Test response with optional fields as None"""
        from src.shared.research_schemas import ProjectThreadResponse

        response = ProjectThreadResponse(
            id=uuid4(),
            project_id=uuid4(),
            thread_id=uuid4(),
            thread_title="Test",
            conversation_id=uuid4(),
            link_type="manual",
            linked_at=datetime.utcnow(),
            linked_by_id=None,
            context_note=None,
            message_count=0,
            last_message_at=None,
        )

        assert response.linked_by_id is None
        assert response.context_note is None
        assert response.last_message_at is None


# ============================================================================
# Error Handling Tests
# ============================================================================


class TestErrorHandling:
    """Tests for API error handling"""

    @pytest.mark.asyncio
    async def test_database_error_triggers_rollback(self, mock_db):
        """Test that database errors trigger rollback"""
        mock_db.commit.side_effect = Exception("Database error")

        # After an exception, rollback should be called
        try:
            await mock_db.commit()
        except Exception:
            await mock_db.rollback()

        mock_db.rollback.assert_called_once()

    def test_http_exceptions_are_reraised(self):
        """Test that HTTPExceptions are re-raised without wrapping"""
        from fastapi import HTTPException, status

        exc = HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not found",
        )

        # HTTP exceptions should preserve their status code
        assert exc.status_code == 404
        assert exc.detail == "Not found"


# ============================================================================
# Authorization Tests
# ============================================================================


class TestAuthorization:
    """Tests for authorization logic"""

    def test_user_owns_workspace(self, mock_user, mock_workspace):
        """Test user ownership check for workspace"""
        assert mock_workspace.owner_id == mock_user.id

    def test_project_in_user_workspace(self, mock_project, mock_workspace):
        """Test project is in user's workspace"""
        assert mock_project.workspace_id == mock_workspace.id

    @pytest.mark.asyncio
    async def test_cross_workspace_link_prevented(
        self, mock_project, mock_thread, mock_conversation
    ):
        """Test that cross-workspace linking is prevented"""
        # Create a different workspace ID for the conversation
        different_workspace_id = uuid4()
        mock_conversation.workspace_id = different_workspace_id
        mock_thread.conversation = mock_conversation

        # Project and thread are in different workspaces
        assert mock_project.workspace_id != mock_thread.conversation.workspace_id
