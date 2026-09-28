"""Bibliography Formatting Service for Research Assistant.

Supports multiple citation formats:
- BibTeX (using pybtex library)
- IEEE
- APA
- MLA
"""

from io import StringIO
from typing import Any, Dict, List

import structlog

# Optional pybtex import for BibTeX formatting
try:
    from pybtex.database import BibliographyData, Entry

    PYBTEX_AVAILABLE = True
except ImportError:
    PYBTEX_AVAILABLE = False
    BibliographyData = None
    Entry = None

from src.models import Citation

logger = structlog.get_logger()


class BibliographyService:
    """Service for formatting bibliographies in various citation styles."""

    @staticmethod
    def _generate_bibtex_key(citation: Citation, index: int) -> str:
        """Generate a BibTeX citation key.

        Args:
            citation: Citation model
            index: Citation index for uniqueness

        Returns:
            BibTeX key (e.g., "smith2023machine")
        """
        # Use first author's last name. authors/document_title are nullable
        # (external-reference citations may carry only a DOI/arXiv id) and an
        # author entry may be an empty string — guard like the ieee/apa/mla
        # formatters do.
        first_author = ""
        if citation.authors and len(citation.authors) > 0:
            author_words = (citation.authors[0] or "").split()
            if author_words:
                first_author = author_words[-1].lower()

        # Use year
        year = str(citation.year) if citation.year else "n.d."

        # Use first word of title
        title_words = (citation.document_title or "").lower().split()
        first_word = title_words[0] if title_words else "paper"

        # Remove special characters
        first_author = "".join(c for c in first_author if c.isalnum())
        first_word = "".join(c for c in first_word if c.isalnum())

        return f"{first_author}{year}{first_word}{index}"

    @staticmethod
    def format_bibtex(citations: List[Citation], keys: List[str] | None = None) -> str:
        """Format citations as BibTeX.

        Args:
            citations: List of Citation models

        Returns:
            BibTeX formatted string
        """
        if not PYBTEX_AVAILABLE:
            raise ImportError(
                "pybtex library not installed. Install with: pip install pybtex==0.24.0"
            )

        if not citations:
            return ""
        if keys is not None and len(keys) != len(citations):
            raise ValueError("BibTeX keys must match the citation count")

        entries = {}

        for i, citation in enumerate(citations, start=1):
            key = (
                keys[i - 1]
                if keys
                else BibliographyService._generate_bibtex_key(citation, i)
            )

            # Determine entry type
            entry_type = "article"
            if citation.arxiv_id:
                entry_type = (
                    "misc"  # ArXiv papers are typically 'misc' or 'unpublished'
                )

            # Build fields
            fields = {}

            if citation.document_title:
                fields["title"] = citation.document_title

            if citation.authors:
                # Format: "Author1 and Author2 and Author3"
                fields["author"] = " and ".join(citation.authors)

            if citation.year:
                fields["year"] = str(citation.year)

            if citation.venue:
                if entry_type == "article":
                    fields["journal"] = citation.venue
                else:
                    fields["booktitle"] = citation.venue

            if citation.doi:
                fields["doi"] = citation.doi

            if citation.arxiv_id:
                fields["eprint"] = citation.arxiv_id
                fields["archivePrefix"] = "arXiv"

            if citation.abstract:
                fields["abstract"] = citation.abstract

            # Create entry
            entry = Entry(entry_type, fields=fields)
            entries[key] = entry

        # Create bibliography
        bib_data = BibliographyData(entries=entries)

        # Format as BibTeX string using to_string instead of to_file
        # to_file closes the file handle which causes issues with StringIO
        bibtex_str = bib_data.to_string("bibtex")

        logger.info("bibliography_generated", format="bibtex", count=len(citations))

        return bibtex_str

    @staticmethod
    def format_ieee(citations: List[Citation]) -> str:
        """Format citations as IEEE style.

        IEEE format:
        [1] A. Author, "Title," Journal, vol. X, no. Y, pp. Z, Year.

        Args:
            citations: List of Citation models

        Returns:
            IEEE formatted string
        """
        lines = []

        for i, citation in enumerate(citations, start=1):
            parts = [f"[{i}]"]

            # Authors
            if citation.authors:
                if len(citation.authors) == 1:
                    parts.append(f"{citation.authors[0]},")
                elif len(citation.authors) == 2:
                    parts.append(f"{citation.authors[0]} and {citation.authors[1]},")
                else:
                    # Use et al. for 3+ authors
                    parts.append(f"{citation.authors[0]} et al.,")

            # Title
            if citation.document_title:
                parts.append(f'"{citation.document_title},"')

            # Venue
            if citation.venue:
                parts.append(f"{citation.venue},")

            # Year
            if citation.year:
                parts.append(f"{citation.year}.")

            # DOI
            if citation.doi:
                parts.append(f"doi: {citation.doi}")

            # ArXiv
            if citation.arxiv_id:
                parts.append(f"arXiv: {citation.arxiv_id}")

            lines.append(" ".join(parts))

        logger.info("bibliography_generated", format="ieee", count=len(citations))

        return "\n\n".join(lines)

    @staticmethod
    def format_apa(citations: List[Citation]) -> str:
        """Format citations as APA 7th edition style.

        APA format:
        Author, A. (Year). Title. Journal, Volume(Issue), pages. https://doi.org/...

        Args:
            citations: List of Citation models

        Returns:
            APA formatted string
        """
        lines = []

        for citation in citations:
            parts = []

            # Authors
            if citation.authors:
                author_parts = []
                for author in citation.authors:
                    # Split into first and last name
                    name_parts = author.split()
                    if len(name_parts) > 1:
                        # Format: Last, F. M.
                        last = name_parts[-1]
                        initials = ". ".join([n[0] for n in name_parts[:-1]]) + "."
                        author_parts.append(f"{last}, {initials}")
                    else:
                        author_parts.append(author)

                if len(author_parts) > 1:
                    authors_str = (
                        ", ".join(author_parts[:-1]) + f", & {author_parts[-1]}"
                    )
                else:
                    authors_str = author_parts[0]
                parts.append(authors_str)

            # Year
            if citation.year:
                parts.append(f"({citation.year}).")

            # Title
            if citation.document_title:
                parts.append(f"{citation.document_title}.")

            # Venue
            if citation.venue:
                parts.append(f"*{citation.venue}*.")

            # DOI
            if citation.doi:
                parts.append(f"https://doi.org/{citation.doi}")
            # ArXiv as alternative
            elif citation.arxiv_id:
                parts.append(f"arXiv:{citation.arxiv_id}")

            lines.append(" ".join(parts))

        logger.info("bibliography_generated", format="apa", count=len(citations))

        return "\n\n".join(lines)

    @staticmethod
    def format_mla(citations: List[Citation]) -> str:
        """Format citations as MLA 9th edition style.

        MLA format:
        Author. "Title." Journal, vol. X, no. Y, Year, pp. Z.

        Args:
            citations: List of Citation models

        Returns:
            MLA formatted string
        """
        lines = []

        for citation in citations:
            parts = []

            # Authors
            if citation.authors:
                if len(citation.authors) == 1:
                    parts.append(f"{citation.authors[0]}.")
                else:
                    # First author: Last, First. Others: First Last.
                    parts.append(f"{citation.authors[0]}, et al.")

            # Title
            if citation.document_title:
                parts.append(f'"{citation.document_title}."')

            # Venue
            if citation.venue:
                parts.append(f"*{citation.venue}*,")

            # Year
            if citation.year:
                parts.append(f"{citation.year}.")

            # DOI or ArXiv
            if citation.doi:
                parts.append(f"doi:{citation.doi}.")
            elif citation.arxiv_id:
                parts.append(f"arXiv:{citation.arxiv_id}.")

            lines.append(" ".join(parts))

        logger.info("bibliography_generated", format="mla", count=len(citations))

        return "\n\n".join(lines)

    @staticmethod
    def format_bibliography(citations: List[Citation], format_type: str) -> str:
        """Format bibliography in specified format.

        Args:
            citations: List of Citation models
            format_type: Format type ("bibtex", "ieee", "apa", "mla")

        Returns:
            Formatted bibliography string

        Raises:
            ValueError: If format_type is not supported
        """
        format_type = format_type.lower()

        if format_type == "bibtex":
            return BibliographyService.format_bibtex(citations)
        elif format_type == "ieee":
            return BibliographyService.format_ieee(citations)
        elif format_type == "apa":
            return BibliographyService.format_apa(citations)
        elif format_type == "mla":
            return BibliographyService.format_mla(citations)
        else:
            raise ValueError(
                f"Unsupported format: {format_type}. Supported: bibtex, ieee, apa, mla"
            )
