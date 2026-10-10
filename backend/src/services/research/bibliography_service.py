"""Bibliography Formatting Service for Research Assistant.

Supports multiple citation formats:
- BibTeX (using pybtex library)
- IEEE
- APA
- MLA
- CSL JSON and RIS reference files (GOO-317)
"""

import json
import re
import unicodedata
from io import StringIO
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

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

CSL_JSON_MIME = "application/vnd.citationstyles.csl+json"
RIS_MIME = "application/x-research-info-systems"

# GOO-317: record ``type`` -> (CSL type, RIS type). Anything else falls back
# to the venue/identifier rules in ``_types`` and then to a generic
# ``document``/``GEN`` record reported as ``type_unmapped``; never guessed.
_TYPE_MAP: Dict[str, Tuple[str, str]] = {
    "journal_article": ("article-journal", "JOUR"),
    "article-journal": ("article-journal", "JOUR"),
    "conference_paper": ("paper-conference", "CONF"),
    "book": ("book", "BOOK"),
    "chapter": ("chapter", "CHAP"),
    "report": ("report", "RPRT"),
    "thesis": ("thesis", "THES"),
    "dataset": ("dataset", "DATA"),
    "webpage": ("webpage", "ELEC"),
    "preprint": ("article", "UNPB"),
}
_GENERIC = ("document", "GEN")
_LINE_BREAKS = re.compile(r"[\r\n]+")


def _get(record: Any, name: str) -> Any:
    """One accessor for canonical records: ``SimpleNamespace`` (ad-hoc draft
    exports) or a GOO-315 snapshot mapping. ``title`` reads either name."""
    names = ("document_title", "title") if name == "title" else (name,)
    for field in names:
        value = (
            record.get(field)
            if isinstance(record, Mapping)
            else getattr(record, field, None)
        )
        if isinstance(value, str):
            value = unicodedata.normalize("NFC", value).strip()
        if value not in (None, ""):
            return value
    return None


def _authors(record: Any) -> List[Any]:
    """Author entries in order. Strings stay whole (never split into
    family/given: ``_canonical_author_names`` already flattened them, so a
    split would be a guess). ``{family, given}`` mappings stay structured.
    ``# ponytail: name parsing is out of scope; literals are lossless.``"""
    out: List[Any] = []
    for author in _get(record, "authors") or []:
        if isinstance(author, Mapping):
            name = {
                k: unicodedata.normalize("NFC", str(author[k])).strip()
                for k in ("family", "given")
                if author.get(k)
            }
            if name:
                out.append(name)
        elif str(author).strip():
            out.append(unicodedata.normalize("NFC", str(author)).strip())
    return out


def _types(record: Any) -> Tuple[str, str, Optional[str]]:
    """(CSL type, RIS type, the unmapped raw type or ``None`` when mapped)."""
    raw = _get(record, "type")
    mapped = _TYPE_MAP.get(str(raw).lower()) if raw is not None else None
    if mapped:
        return mapped[0], mapped[1], None
    if _get(record, "venue") and _get(record, "doi"):
        return "article-journal", "JOUR", None
    if _get(record, "arxiv_id") and not (_get(record, "venue") or _get(record, "doi")):
        return "article", "UNPB", None
    return _GENERIC[0], _GENERIC[1], str(raw) if raw is not None else None


def _year(record: Any) -> Any:
    year = _get(record, "year")
    if isinstance(year, str) and year.isdigit():
        return int(year)
    return year


def _checked_keys(records: Sequence[Any], keys: Sequence[str]) -> Sequence[str]:
    if len(keys) != len(records):
        raise ValueError("Reference keys must match the record count")
    return keys


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
    def format_csl_json(records: Sequence[Any], keys: Sequence[str]) -> str:
        """CSL JSON (CSL-data 1.0) for canonical records, ``id`` = ``docN``.

        Only present fields are written; ``omissions`` lists the rest. No
        abstract, no snippet: a missing title stays missing.
        ``# ponytail: abstracts/keywords and citeproc rendering are out of
        scope; add them here if a pilot asks.``
        """
        items = []
        for record, key in zip(records, _checked_keys(records, keys)):
            csl_type, _ris, _unmapped = _types(record)
            item: Dict[str, Any] = {"id": key, "type": csl_type}
            title = _get(record, "title")
            if title:
                item["title"] = title
            authors = _authors(record)
            if authors:
                item["author"] = [
                    a if isinstance(a, dict) else {"literal": a} for a in authors
                ]
            year = _year(record)
            if year is not None:
                item["issued"] = {"date-parts": [[year]]}
            venue = _get(record, "venue")
            if venue:
                item["container-title"] = venue
            doi = _get(record, "doi")
            if doi:
                item["DOI"] = doi
            arxiv_id = _get(record, "arxiv_id")
            if arxiv_id:
                item["archive"] = "arXiv"
                item["archive_location"] = str(arxiv_id)
            items.append(item)
        logger.info("bibliography_generated", format="csl-json", count=len(items))
        return json.dumps(items, ensure_ascii=False, sort_keys=True, indent=2) + "\n"

    @staticmethod
    def format_ris(records: Sequence[Any], keys: Sequence[str]) -> str:
        """RIS for canonical records: UTF-8 (no BOM), CRLF, NFC, ``ID`` = ``docN``."""

        def line(tag: str, value: Any) -> str:
            text = _LINE_BREAKS.sub(" ", str(value)).strip()
            return f"{tag}  - {text}"

        blocks = []
        for record, key in zip(records, _checked_keys(records, keys)):
            _csl, ris_type, _unmapped = _types(record)
            lines = [line("TY", ris_type), line("ID", key)]
            title = _get(record, "title")
            if title:
                lines.append(line("TI", title))
            for author in _authors(record):
                if isinstance(author, dict):
                    author = ", ".join(
                        author[k] for k in ("family", "given") if k in author
                    )
                lines.append(line("AU", author))
            year = _year(record)
            if year is not None:
                lines.append(line("PY", year))
            venue = _get(record, "venue")
            if venue:
                lines.append(line("T2", venue))
                if ris_type == "JOUR":
                    lines.append(line("JO", venue))
            doi = _get(record, "doi")
            if doi:
                lines.append(line("DO", doi))
            arxiv_id = _get(record, "arxiv_id")
            if arxiv_id:
                lines.append(line("AN", f"arXiv:{arxiv_id}"))
            lines.append("ER  - ")
            blocks.append("\r\n".join(lines) + "\r\n")
        logger.info("bibliography_generated", format="ris", count=len(blocks))
        return "\r\n".join(blocks)

    @staticmethod
    def omissions(records: Sequence[Any], keys: Sequence[str]) -> List[Dict[str, Any]]:
        """Every field a CSL/RIS export leaves out, so nothing is silently
        dropped: ``{key, field, reason: absent, value: None}`` for a missing
        title, authors, year, venue or identifier (DOI and arXiv id), and
        ``{key, field: type, reason: type_unmapped, value}`` for a generic
        record."""
        out: List[Dict[str, Any]] = []
        for record, key in zip(records, _checked_keys(records, keys)):
            csl_type, _ris, unmapped = _types(record)
            if csl_type == _GENERIC[0]:
                out.append(
                    {
                        "key": key,
                        "field": "type",
                        "reason": "type_unmapped",
                        "value": unmapped,
                    }
                )
            present = {
                "title": _get(record, "title"),
                "authors": _authors(record),
                "year": _year(record),
                "venue": _get(record, "venue"),
                "identifier": _get(record, "doi") or _get(record, "arxiv_id"),
            }
            out += [
                {"key": key, "field": field, "reason": "absent", "value": None}
                for field, value in present.items()
                if value in (None, "", [])
            ]
        return out

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


# GOO-317: reference-file format -> (serializer, download filename, MIME).
REFERENCE_FILES: Dict[
    str, Tuple[Callable[[Sequence[Any], Sequence[str]], str], str, str]
] = {
    "csl-json": (BibliographyService.format_csl_json, "references.json", CSL_JSON_MIME),
    "ris": (BibliographyService.format_ris, "references.ris", RIS_MIME),
}
