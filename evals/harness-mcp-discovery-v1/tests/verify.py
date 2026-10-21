#!/usr/bin/env python3
"""Deterministic verifier for ``harness-mcp-discovery-v1``.

Exit 0 = capability pass, 10 = scoreable capability failure, 2 = verifier or
infrastructure failure (``test.sh`` then writes no reward). There is no judge:
semantic scoring is N/A and every check below is literal.

Question under test (Plan 07 Slice 5): can an external harness (Claude Code or
Codex) combine the NOUS MCP primitives ``search_documents``, ``search_arxiv``
and ``search_external_database`` into one literature scan, without a
composite ``discover_papers`` tool?

Inputs:
- evidence: ``/app/answer.md`` (existence, file type and text), captured by
  this script into ``/logs/verifier/evidence.json`` before scoring. A symlink,
  hard link or special file is recorded but never read;
- state: ``GET http://mock-nous:8080/events``, the mock gateway's read-only log
  of every ``/api/v1/integrations/*`` request any client in ``main`` made.

Bridge traffic: the real bridge runs in a separate service whose private
bearer is mounted only there and in the mock gateway. The gateway derives
``bridge_authenticated`` from exact credential validation. The agent's stdio
relay carries MCP bytes to the real bridge; choosing Node's User-Agent cannot
authenticate a direct gateway request. User-Agent is diagnostic only.

Infrastructure (exit 2, never reward 0): the mock is unreachable; no
successful bridge ``GET /api/v1/integrations/tools`` was recorded (the MCP
server never connected); a bridge request lacked either auth header.

Objective failures (exit 10): any integration request did not come from the
bridge; a required search tool was never accepted, through the bridge, with a
non-empty ``query``; the answer is missing, empty or not a regular file; the
answer cites no planted library document (id or exact title), no planted arXiv
id, or no planted PMID/DOI; the answer contains an arXiv-style id, PMID, PMCID,
DOI or UUID that its required search never served (fabrication); a paper-list
row does not bind one returned paper's title, identifier and explicit source.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import unicodedata
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, "/opt/nous-eval/lib")
try:
    from evals.harbor_common.envelope import InfrastructureFailure, run_verifier_main
except ImportError:  # pragma: no cover - local calibration path
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from evals.harbor_common.envelope import InfrastructureFailure, run_verifier_main

BENCHMARK_ID = "harness-mcp-discovery-v1"
TRUTH_PATH = Path(__file__).with_name("truth.json")
try:
    TRUTH = json.loads(TRUTH_PATH.read_text(encoding="utf-8"))
except FileNotFoundError:  # pragma: no cover - local calibration without /tests
    TRUTH = json.loads(
        (Path(__file__).resolve().parent / "truth.json").read_text(encoding="utf-8")
    )

ANSWER_PATH = Path(os.environ.get("ANSWER_PATH", TRUTH["answer_path"]))
EVENTS_URL = os.environ.get("MOCK_EVENTS_URL", TRUTH["mock_events_url"])
CATALOG_PATH = TRUTH["catalog_path"]
READ_PATH = TRUTH["read_path"]
INTEGRATION_PREFIX = "/api/v1/integrations/"
REQUIRED_TOOLS = tuple(TRUTH["required_search_tools"])
CORPUS_IDS = {value.lower() for value in TRUTH["corpus"]["document_ids"]}
SERVED_UUIDS = {value.lower() for value in TRUTH["served_uuids"]}
ARXIV_DOI_PREFIX = "10.48550/arxiv."
MAX_ANSWER_BYTES = 1_000_000

# An arXiv-style id not glued to a preceding letter, digit or dot (so DOI
# fragments such as ``nouseval.2025.40517001`` do not count), plus ids glued to
# ``arXiv`` (``arXiv2005.11401``) or written as arXiv DOIs
# (``10.48550/arXiv.2509.17001``). ``_`` and ``/`` may precede (markdown
# italics, ``arXiv_``, ``abs/``). The version suffix is optional.
ARXIV_RE = re.compile(
    r"(?:(?<![A-Za-z0-9.])|(?<=arxiv)|(?<=arxiv\.))(\d{4}\.\d{4,5})(?:v\d+)?(?!\d)",
    re.IGNORECASE,
)
# PMIDs: after a PMID label (``PMID``, ``PMIDs:``, ``pmid=``; every number of a
# following list), after a PubMed label (``PubMed:``, ``PubMed ID``; 5+ digits
# so a year is not read as an id), in PubMed URLs, and any standalone 7-9 digit
# number on a line that mentions PubMed/PMID/MEDLINE or in a table whose header
# row does.
_PMID_LIST = r"(?:\s*(?:,|;|/|&|\band\b)\s*\d{5,9}(?!\d))*"
PMID_LABEL_RE = re.compile(
    r"\bPMIDs?(?![A-Za-z])\s*(?:[:#=]\s*)?(\d{1,9}(?!\d)" + _PMID_LIST + r")"
    r"|\bPubMed(?:\s+IDs?)?\b\s*(?:[:#=]\s*)?(\d{5,9}(?!\d)" + _PMID_LIST + r")",
    re.IGNORECASE,
)
PUBMED_URL_RE = re.compile(
    r"(?:pubmed\.ncbi\.nlm\.nih\.gov/|ncbi\.nlm\.nih\.gov/pubmed/)(\d{1,9})(?!\d)",
    re.IGNORECASE,
)
PUBMED_MENTION_RE = re.compile(r"\bpubmed\b|\bpmids?\b|\bmedline\b", re.IGNORECASE)
STANDALONE_NUMBER_RE = re.compile(r"(?<![\w./-])(\d{7,9})(?![\w/-]|\.\d)")
PMCID_RE = re.compile(r"\bPMC\d{4,9}(?!\d)", re.IGNORECASE)
DOI_RE = re.compile(r"(?<![\w.])10\.\d{4,9}/[^\s|)\]>,;\"'`<]+", re.IGNORECASE)
UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE
)
# Markdown backslash escapes of ASCII punctuation (``Ward\-Round``).
MD_ESCAPE_RE = re.compile(r"\\([!-/:-@\[-`{-~])")
LIST_ROW_RE = re.compile(r"^(?:[-*+]|\d+[.)])\s+\S")
TABLE_SEPARATOR_RE = re.compile(r"^\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?$")
RULE_RE = re.compile(r"^(?:[-*_]\s*){3,}$")
_DASHES = dict.fromkeys(map(ord, "‐‑‒–—−"), "-")
_QUOTES = {
    **dict.fromkeys(map(ord, "‘’‚‛′ʼ"), "'"),
    **dict.fromkeys(map(ord, "“”„‟″"), '"'),
}


# --------------------------------------------------------------------------
# inputs
# --------------------------------------------------------------------------
def capture_answer() -> dict[str, Any]:
    """Evidence from the agent's workspace; never trusted for state.

    Only a regular file with a single link is read, and never through a
    symlink: ``tests/`` reaches ``/tests`` after the agent finishes, so a link
    planted at the answer path could otherwise resolve to ``truth.json``.
    """
    try:
        info: os.stat_result | None = os.lstat(ANSWER_PATH)
    except (FileNotFoundError, NotADirectoryError):
        info = None
    regular = bool(info and stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
    raw = b""
    size = info.st_size if info else 0
    if regular:
        fd = os.open(ANSWER_PATH, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            opened = os.fstat(handle.fileno())
            regular = stat.S_ISREG(opened.st_mode) and opened.st_nlink == 1
            size = opened.st_size
            if regular:
                raw = handle.read(MAX_ANSWER_BYTES)
    return {
        "schema_version": "1.0",
        "benchmark_id": BENCHMARK_ID,
        "answer_path": str(ANSWER_PATH),
        "answer_exists": info is not None,
        "answer_is_regular_file": regular,
        "answer_file_mode": stat.filemode(info.st_mode) if info else None,
        "answer_bytes": size,
        "answer_truncated": size > MAX_ANSWER_BYTES,
        "answer": raw.decode("utf-8", errors="replace"),
    }


def live_state() -> dict[str, Any]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(EVENTS_URL, timeout=10) as response:
            events = json.loads(response.read().decode("utf-8"))
    except Exception as error:  # noqa: BLE001 - any read failure is infrastructure
        raise InfrastructureFailure(
            f"mock-nous event log unreachable: {error}"
        ) from error
    if not isinstance(events, list):
        raise InfrastructureFailure("mock-nous /events did not return a JSON list")
    return {"events": events}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def integration_events(state: dict[str, Any]) -> list[dict[str, Any]]:
    events = state.get("events")
    if not isinstance(events, list):
        raise InfrastructureFailure("state carries no mock-nous event list")
    return [
        event
        for event in events
        if isinstance(event, dict)
        and str(event.get("path") or "").startswith(INTEGRATION_PREFIX)
    ]


def split_by_client(
    events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(bridge requests, requests from any other client)."""
    bridge = [e for e in events if e.get("bridge_authenticated") is True]
    other = [e for e in events if e.get("bridge_authenticated") is not True]
    return bridge, other


def accepted_reads(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Successful tools/read results, in log order, excluding error envelopes."""
    return sorted(
        (
            event
            for event in events
            if event.get("method") == "POST"
            and event.get("path") == READ_PATH
            and event.get("status") == 200
            and event.get("is_error") is False
            and isinstance(event.get("tool_name"), str)
        ),
        key=lambda event: event.get("seq") or 0,
    )


def has_query(event: dict[str, Any]) -> bool:
    arguments = event.get("arguments")
    return (
        isinstance(arguments, dict)
        and isinstance(arguments.get("query"), str)
        and bool(arguments["query"].strip())
    )


def unescape_markdown(text: str) -> str:
    return MD_ESCAPE_RE.sub(r"\1", text)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", unescape_markdown(text))
    text = text.translate(_DASHES).translate(_QUOTES)
    return " ".join(text.split()).casefold()


def _is_table_header(lines: list[str], index: int) -> bool:
    following = lines[index + 1].strip() if index + 1 < len(lines) else ""
    return lines[index].strip().startswith("|") and bool(
        "|" in following and TABLE_SEPARATOR_RE.match(following)
    )


def answer_pmids(answer: str) -> set[str]:
    found: set[str] = set()
    for match in PMID_LABEL_RE.finditer(answer):
        found.update(re.findall(r"\d+", match.group(1) or match.group(2)))
    found.update(match.group(1) for match in PUBMED_URL_RE.finditer(answer))
    lines = answer.splitlines()
    pubmed_table = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if _is_table_header(lines, index):
            pubmed_table = bool(PUBMED_MENTION_RE.search(stripped))
        elif not stripped.startswith("|"):
            pubmed_table = False
        if pubmed_table or PUBMED_MENTION_RE.search(line):
            found.update(m.group(1) for m in STANDALONE_NUMBER_RE.finditer(line))
    return found


def answer_dois(answer: str) -> set[str]:
    return {
        match.group(0).rstrip(".,;:!?*_~'\"").lower()
        for match in DOI_RE.finditer(answer)
    }


def doi_served(doi: str, sources: dict[str, set[str]]) -> bool:
    if doi.startswith(ARXIV_DOI_PREFIX):
        return re.sub(r"v\d+$", "", doi[len(ARXIV_DOI_PREFIX) :]) in sources["arxiv"]
    return doi in sources["dois"]


def answer_ids(answer: str) -> dict[str, set[str]]:
    return {
        "arxiv": {match.group(1) for match in ARXIV_RE.finditer(answer)},
        "pmids": answer_pmids(answer),
        "pmcids": {match.group(0).upper() for match in PMCID_RE.finditer(answer)},
        "dois": answer_dois(answer),
        "uuids": {match.group(0).lower() for match in UUID_RE.finditer(answer)},
    }


def returned_sources(reads: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Bind fixture aliases to papers returned by successful calls in this trial."""
    returned: dict[str, set[str]] = {tool: set() for tool in REQUIRED_TOOLS}
    for event in reads:
        tool = event.get("tool_name")
        if tool not in returned or not has_query(event):
            continue
        ids = event.get("returned_ids")
        if not isinstance(ids, list) or not all(
            isinstance(value, str) for value in ids
        ):
            raise InfrastructureFailure("successful read has malformed returned_ids")
        for value in ids:
            lowered = value.lower()
            match = ARXIV_RE.fullmatch(lowered)
            returned[tool].add(match.group(1) if match else lowered)
    sources: dict[str, set[str]] = {
        key: set()
        for key in (
            "corpus",
            "corpus_titles",
            "arxiv",
            "pmids",
            "dois",
            "titles",
            "uuids",
        )
    }
    # The catalog serves project metadata; it is not a paper citation.
    sources["uuids"].update(SERVED_UUIDS - CORPUS_IDS)
    for paper in TRUTH["papers"]:
        if paper["returned_id"].lower() not in returned[paper["tool_name"]]:
            continue
        sources["titles"].add(paper["title"])
        for field, kind in (
            ("document_id", "corpus"),
            ("arxiv_id", "arxiv"),
            ("pmid", "pmids"),
            ("doi", "dois"),
        ):
            if field in paper:
                sources[kind].add(paper[field].lower())
        if "document_id" in paper:
            sources["uuids"].add(paper["document_id"].lower())
            sources["corpus_titles"].add(paper["title"])
    return sources


def field_text(value: str) -> str:
    """Compare displayed Markdown fields, preserving the paper's words."""
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    value = value.replace("**", "").replace("__", "").replace("`", "")
    return normalize(value).strip(" *_\"'")


def paper_identifiers(text: str) -> set[str]:
    found = answer_ids(text)
    cited = set().union(*(found[k] for k in ("uuids", "arxiv", "pmids", "dois")))
    for identifier in tuple(cited):
        if identifier.startswith(ARXIV_DOI_PREFIX):
            match = ARXIV_RE.fullmatch(identifier[len(ARXIV_DOI_PREFIX) :])
            if match:
                cited.remove(identifier)
                cited.add(ARXIV_DOI_PREFIX + match.group(1))
    return cited


def matching_paper(text: str, sources: dict[str, set[str]]) -> dict[str, Any] | None:
    """Bind three explicit fields to one actually returned search record."""
    row = re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", text.strip())
    cells = [cell.strip() for cell in row.strip("|").split("|")]
    if len(cells) != 3:
        return None
    title, identifier_field, source = cells
    title, source = field_text(title), field_text(source)
    source_labels = {
        "search_documents": {"nous library", "library", "nous"},
        "search_arxiv": {"arxiv"},
        "search_external_database": {
            "pubmed",
            "medline",
            "pubmed/medline",
            "pubmed / medline",
        },
    }
    # A bare PMID is interpreted in its explicit PubMed source field.
    identifier_ids = paper_identifiers(identifier_field + " " + source)
    all_ids = paper_identifiers(text)
    for paper in TRUTH["papers"]:
        if (
            paper["title"] not in sources["titles"]
            or title != normalize(paper["title"])
            or source not in source_labels[paper["tool_name"]]
        ):
            continue
        identifiers = {
            paper[field].lower()
            for field in ("document_id", "arxiv_id", "pmid", "doi")
            if field in paper
        }
        if "arxiv_id" in paper:
            identifiers.add(ARXIV_DOI_PREFIX + paper["arxiv_id"].lower())
        if identifier_ids and identifier_ids <= identifiers and all_ids <= identifiers:
            return cast(dict[str, Any], paper)
    return None


def cites_served(text: str, sources: dict[str, set[str]]) -> bool:
    return matching_paper(text, sources) is not None


def paper_list_rows(answer: str) -> list[list[str]]:
    """List items, including nested items, and table rows grouped into blocks.

    A row starts at a ``-``/``*``/``+``/``1.`` item or ``|`` table
    row and absorbs wrapped prose lines below it. Nested items are new rows.
    A heading, horizontal rule or unindented paragraph line ends a block.
    Table header and separator rows are not rows.
    """
    lines = answer.splitlines()
    blocks: list[list[str]] = []
    block: list[str] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if (
            line[0] in " \t"
            and not LIST_ROW_RE.match(stripped)
            and not stripped.startswith("|")
        ):
            if block:
                block[-1] += "\n" + stripped
            continue
        if stripped.startswith("|") and TABLE_SEPARATOR_RE.match(stripped):
            continue
        is_row = stripped.startswith("|") or bool(LIST_ROW_RE.match(stripped))
        if (
            is_row
            and not RULE_RE.match(stripped)
            and not _is_table_header(lines, index)
        ):
            block.append(stripped)
            continue
        if block:
            blocks.append(block)
        block = []
    if block:
        blocks.append(block)
    return blocks


def answer_audit(answer: str, reads: list[dict[str, Any]]) -> dict[str, Any]:
    sources = returned_sources(reads)
    answer = unescape_markdown(answer)
    lowered = answer.lower()
    normalized = normalize(answer)
    found = answer_ids(answer)
    matched = [
        paper
        for block in paper_list_rows(answer)
        for row in block
        if (paper := matching_paper(row, sources)) is not None
    ]
    uncited_rows = [
        row.splitlines()[0][:160]
        for block in paper_list_rows(answer)
        for row in block
        if not cites_served(row, sources)
    ]
    return {
        "corpus_ids_cited": sorted(
            value for value in sources["corpus"] if value in lowered
        ),
        "corpus_titles_cited": sorted(
            title
            for title in sources["corpus_titles"]
            if normalize(title) in normalized
        ),
        "arxiv_ids_cited": sorted(found["arxiv"] & sources["arxiv"]),
        "pmids_cited": sorted(
            pmid
            for pmid in sources["pmids"]
            if re.search(rf"(?<!\d){pmid}(?!\d)", answer)
        ),
        "dois_cited": sorted(doi for doi in sources["dois"] if doi in lowered),
        "fabricated_arxiv_ids": sorted(found["arxiv"] - sources["arxiv"]),
        "fabricated_pmids": sorted(found["pmids"] - sources["pmids"]),
        "fabricated_pmcids": sorted(found["pmcids"]),
        "fabricated_dois": sorted(
            doi for doi in found["dois"] if not doi_served(doi, sources)
        ),
        "fabricated_uuids": sorted(found["uuids"] - sources["uuids"]),
        "uncited_paper_rows": uncited_rows,
        "matched_search_tools": sorted({paper["tool_name"] for paper in matched}),
    }


# --------------------------------------------------------------------------
# gate
# --------------------------------------------------------------------------
def check_infrastructure(bridge_events: list[dict[str, Any]]) -> None:
    catalog = [
        event
        for event in bridge_events
        if event.get("method") == "GET"
        and event.get("path") == CATALOG_PATH
        and event.get("status") == 200
    ]
    if not catalog:
        raise InfrastructureFailure(
            "no successful bridge GET /api/v1/integrations/tools recorded "
            "(private bridge authentication): the NOUS MCP server never listed "
            "its tools, so the harness never connected"
        )
    unauthenticated = [
        event.get("seq")
        for event in bridge_events
        if not (
            isinstance(event.get("headers_present"), dict)
            and event["headers_present"].get("authorization") is True
            and event["headers_present"].get("grant") is True
        )
    ]
    if unauthenticated:
        raise InfrastructureFailure(
            "bridge requests without both Authorization and "
            f"X-NOUS-Integration-Grant headers: seq {unauthenticated}"
        )
    if any(
        isinstance(event.get("status"), int) and event["status"] >= 500
        for event in bridge_events
    ):
        raise InfrastructureFailure(
            "gateway/application error: bridge HTTP server failure"
        )


def objective_failures(evidence: dict[str, Any], state: dict[str, Any]) -> list[str]:
    bridge, other = split_by_client(integration_events(state))
    check_infrastructure(bridge)
    failures: list[str] = []

    if other:
        agents = sorted({str(event.get("user_agent")) for event in other})
        failures.append(
            "integration requests bypassed the NOUS MCP bridge (User-Agent "
            f"{agents}): seq {[event.get('seq') for event in other]}"
        )
    reads = accepted_reads(bridge)
    for tool in REQUIRED_TOOLS:
        errors = [
            event
            for event in bridge
            if event.get("method") == "POST"
            and event.get("path") == READ_PATH
            and event.get("tool_name") == tool
            and has_query(event)
            and event.get("status") == 200
            and event.get("is_error") is not False
        ]
        if errors:
            raise InfrastructureFailure(
                f"{tool} returned a gateway/application error; the trial is infrastructure-degraded"
            )
        if not any(event["tool_name"] == tool and has_query(event) for event in reads):
            failures.append(
                f"{tool} was never called through the bridge with a non-empty query"
            )

    if (
        evidence.get("answer_exists")
        and evidence.get("answer_is_regular_file") is False
    ):
        failures.append(
            f"answer is not a regular, singly linked file ({evidence.get('answer_file_mode')}): "
            f"{evidence.get('answer_path')} was not read"
        )
        return failures
    raw_answer = evidence.get("answer")
    answer = raw_answer if isinstance(raw_answer, str) else ""
    size = evidence.get("answer_bytes")
    if evidence.get("answer_truncated") is True or (
        isinstance(size, int) and size > MAX_ANSWER_BYTES
    ):
        failures.append("answer exceeded the capture limit and cannot be fully audited")
        return failures
    if not evidence.get("answer_exists") or not answer.strip():
        failures.append(f"answer missing or empty: {evidence.get('answer_path')}")
        return failures

    audit = answer_audit(answer, reads)
    for tool in REQUIRED_TOOLS:
        if tool not in audit["matched_search_tools"]:
            failures.append(f"no complete paper row matches a result from {tool}")
    if not (audit["corpus_ids_cited"] or audit["corpus_titles_cited"]):
        failures.append(
            "answer cites no planted NOUS library document (document id or exact title)"
        )
    if not audit["arxiv_ids_cited"]:
        failures.append("answer cites no planted arXiv preprint id")
    if not (audit["pmids_cited"] or audit["dois_cited"]):
        failures.append("answer cites no planted PubMed article (PMID or DOI)")
    for kind in (
        "fabricated_arxiv_ids",
        "fabricated_pmids",
        "fabricated_pmcids",
        "fabricated_dois",
        "fabricated_uuids",
    ):
        if audit[kind]:
            failures.append(
                f"answer cites identifiers not returned by their required searches ({kind}): {audit[kind]}"
            )
    if audit["uncited_paper_rows"]:
        failures.append(
            "paper-list rows do not bind title, identifier and source to one "
            f"required-search result: {audit['uncited_paper_rows']}"
        )
    return failures


objective_failures.live_reader = live_state  # type: ignore[attr-defined]


def report_extra(evidence: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Diagnostics only; nothing here changes the verdict."""
    events = integration_events(state)
    bridge, other = split_by_client(events)
    reads = accepted_reads(bridge)
    rejected = [
        event
        for event in bridge
        if event.get("method") == "POST"
        and event.get("path") == READ_PATH
        and event.get("status") != 200
    ]
    first_calls: list[str] = []
    for event in reads:
        if event["tool_name"] not in first_calls:
            first_calls.append(event["tool_name"])
    external_calls = [
        {
            "seq": event.get("seq"),
            "query": (event.get("arguments") or {}).get("query"),
            "connector": (event.get("arguments") or {}).get("connector"),
            "domain": (event.get("arguments") or {}).get("domain"),
            "filters": (event.get("arguments") or {}).get("filters"),
            "is_error": event.get("is_error"),
            "returned_ids": event.get("returned_ids"),
        }
        for event in reads
        if event["tool_name"] == "search_external_database"
    ]
    returned: set[str] = set()
    for event in reads:
        returned.update(str(value) for value in event.get("returned_ids") or [])
    raw_answer = evidence.get("answer")
    answer = raw_answer if isinstance(raw_answer, str) else ""
    return {
        "tool_call_counts": dict(
            sorted(Counter(e["tool_name"] for e in reads).items())
        ),
        "rejected_call_counts": dict(
            sorted(Counter(str(e.get("tool_name")) for e in rejected).items())
        ),
        "application_error_counts": dict(
            sorted(
                Counter(
                    str(event.get("tool_name"))
                    for event in bridge
                    if event.get("path") == READ_PATH and event.get("is_error") is True
                ).items()
            )
        ),
        "first_call_order": first_calls,
        "search_external_database_calls": external_calls,
        "list_external_databases_called": any(
            event["tool_name"] == "list_external_databases" for event in reads
        ),
        "catalog_fetches": sum(
            1
            for event in bridge
            if event.get("method") == "GET" and event.get("path") == CATALOG_PATH
        ),
        "library_tools_used": sorted(
            {
                event["tool_name"]
                for event in reads
                if event["tool_name"]
                in {"search_documents", "list_project_documents", "retrieve_passages"}
            }
        ),
        "returned_ids": sorted(returned),
        "user_agents": sorted({str(event.get("user_agent")) for event in events}),
        "non_bridge_requests": [
            {
                key: event.get(key)
                for key in (
                    "seq",
                    "method",
                    "path",
                    "tool_name",
                    "status",
                    "user_agent",
                )
            }
            for event in other
        ],
        "answer_audit": answer_audit(answer, reads) if answer else None,
        "answer_lines": len([line for line in answer.splitlines() if line.strip()]),
    }


def main() -> int:
    if not os.environ.get("BENCHMARK_CALIBRATION_FIXTURE"):
        evidence_path = Path(
            os.environ.get("EVIDENCE_PATH", "/logs/verifier/evidence.json")
        )
        try:
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            evidence_path.write_text(
                json.dumps(capture_answer(), indent=2, sort_keys=True), encoding="utf-8"
            )
        except Exception as error:  # noqa: BLE001 - capture failure is infrastructure
            print(
                json.dumps(
                    {"benchmark_id": BENCHMARK_ID, "verifier_error": repr(error)}
                ),
                file=sys.stderr,
            )
            return 2
        os.environ["EVIDENCE_PATH"] = str(evidence_path)
    return cast(int, run_verifier_main(BENCHMARK_ID, objective_failures, report_extra))


if __name__ == "__main__":
    sys.exit(main())
