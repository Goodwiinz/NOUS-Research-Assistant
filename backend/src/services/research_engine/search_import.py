"""Bounded parsers for search results exported from databases without an API.

GOO-300. Pure and stdlib-only. A whole-file problem raises ``ImportFormatError``
(nothing is persisted); a per-record problem yields a ``ParsedRecord`` with a
``rejection_reason`` and its original ``raw`` text, so every parsed chunk is
accounted for as accepted or rejected. The parsers never infer a search query or
search date from the file: those are declared by the importer.
"""

import csv
import io
import re
from dataclasses import dataclass
from typing import Any, Iterator

from src.services.research_engine.discovery import extract_identifiers_from_mapping

PARSER_VERSION = "nous.search-import.v1"
FORMATS = ("ris", "csv", "nbib")
MAX_BYTES, MAX_RECORDS, MAX_RECORD_BYTES = 5 * 1024 * 1024, 5_000, 64 * 1024

_ID_KINDS = ("doi", "pmid", "pmcid", "arxiv")
_CSV_COLUMNS = (
    "title",
    "authors",
    "year",
    "doi",
    "pmid",
    "pmcid",
    "arxiv",
    "abstract",
    "venue",
    "url",
)
_RIS_TAG = re.compile(r"^([A-Z][A-Z0-9])  - ?(.*)$")
_NBIB_TAG = re.compile(r"^([A-Z]{2,4})\s*- (.*)$")
_RIS_MAP = {
    "TI": "title",
    "T1": "title",
    "AU": "authors",
    "A1": "authors",
    "PY": "year",
    "Y1": "year",
    "DA": "year",
    "AB": "abstract",
    "N2": "abstract",
    "DO": "doi",
    "UR": "url",
    "JO": "venue",
    "T2": "venue",
}
_NBIB_MAP = {
    "PMID": "pmid",
    "PMC": "pmcid",
    "TI": "title",
    "FAU": "authors",
    "DP": "year",
    "AB": "abstract",
}


class ImportFormatError(ValueError):
    """The whole file is unusable; the caller persists nothing."""

    def __init__(self, code: str, status: int = 422):
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class ParsedRecord:
    index: int
    raw: str
    parsed: dict[str, Any]
    rejection_reason: str | None


# (raw, mapped values, tag fields or None, chunk-level rejection or None)
_Chunk = tuple[str, dict[str, Any], dict[str, list[str]] | None, str | None]


def _tagged_chunk(
    lines: list[str], pattern: re.Pattern[str], reason: str | None, nbib: bool
) -> _Chunk:
    fields: dict[str, list[str]] = {}
    last: str | None = None
    for line in lines:
        body = line.rstrip("\r\n")
        if not body.strip():
            continue
        match = pattern.match(body)
        if match:
            last = match.group(1)
            fields.setdefault(last, []).append(match.group(2).strip())
        elif nbib and last and body.startswith("      "):
            fields[last][-1] += " " + body.strip()
        else:
            reason = reason or "malformed_line"
    mapping = _NBIB_MAP if nbib else _RIS_MAP
    values: dict[str, Any] = {}
    for tag, key in mapping.items():
        for value in fields.get(tag, []):
            if key == "authors":
                values.setdefault("authors", []).append(value)
            else:
                values.setdefault(key, value)
    if nbib:
        if "authors" not in values and fields.get("AU"):
            values["authors"] = list(fields["AU"])
        for value in fields.get("LID", []) + fields.get("AID", []):
            if value.endswith("[doi]"):
                values.setdefault("doi", value[: -len("[doi]")].strip())
    return "".join(lines), values, fields, reason


def _ris_chunks(text: str) -> Iterator[_Chunk]:
    current: list[str] | None = None
    stray: list[str] = []
    for line in text.splitlines(keepends=True):
        match = _RIS_TAG.match(line.rstrip("\r\n"))
        tag = match.group(1) if match else None
        if tag == "TY":
            if current is not None:
                yield _tagged_chunk(current, _RIS_TAG, "unterminated_record", False)
            elif any(s.strip() for s in stray):
                yield _tagged_chunk(stray, _RIS_TAG, "malformed_line", False)
            current, stray = [line], []
        elif current is not None:
            current.append(line)
            if tag == "ER":
                yield _tagged_chunk(current, _RIS_TAG, None, False)
                current = None
        else:
            stray.append(line)
    if current is not None:
        yield _tagged_chunk(current, _RIS_TAG, "unterminated_record", False)
    elif any(s.strip() for s in stray):
        yield _tagged_chunk(stray, _RIS_TAG, "malformed_line", False)


def _nbib_chunks(text: str) -> Iterator[_Chunk]:
    block: list[str] = []
    for line in text.splitlines(keepends=True) + ["\n"]:
        if line.strip():
            block.append(line)
        elif block:
            yield _tagged_chunk(block, _NBIB_TAG, None, True)
            block = []


def _csv_chunks(text: str) -> Iterator[_Chunk]:
    lines = io.StringIO(text).readlines()
    reader = csv.reader(iter(lines))
    try:
        header = next(reader)
    except StopIteration:
        return
    columns = {name.strip().casefold(): i for i, name in enumerate(header)}
    if "title" not in columns:
        raise ImportFormatError("csv_missing_title_column")
    known = {key: columns[key] for key in _CSV_COLUMNS if key in columns}
    start = reader.line_num
    while True:
        try:
            row = next(reader)
        except StopIteration:
            return
        except csv.Error:
            # ponytail: the csv module cannot resync; the rest is one rejected chunk.
            yield "".join(lines[start:]), {}, None, "malformed_line"
            return
        raw, start = "".join(lines[start : reader.line_num]), reader.line_num
        if not any(cell.strip() for cell in row):
            continue
        values: dict[str, Any] = {
            key: row[i].strip()
            for key, i in known.items()
            if i < len(row) and row[i].strip()
        }
        if "authors" in values:
            values["authors"] = [
                a.strip() for a in values["authors"].split(";") if a.strip()
            ]
        yield raw, values, None, None


def _record(index: int, chunk: _Chunk) -> ParsedRecord:
    raw, values, fields, reason = chunk
    if len(raw.encode("utf-8")) > MAX_RECORD_BYTES:
        truncated = raw.encode("utf-8")[:MAX_RECORD_BYTES].decode("utf-8", "ignore")
        return ParsedRecord(
            index, truncated, {"raw_truncated": True}, "record_too_large"
        )
    candidates = {k: values.pop(k) for k in _ID_KINDS if k in values}
    identifiers = extract_identifiers_from_mapping(candidates)
    parsed: dict[str, Any] = {
        "identifiers": identifiers,
        "dropped_identifiers": [
            {"kind": k, "value": v}
            for k, v in candidates.items()
            if k not in identifiers
        ],
        **values,
    }
    if isinstance(parsed.get("year"), str):
        year = re.search(r"\d{4}", parsed["year"])
        if year:
            parsed["year"] = year.group(0)
        else:
            parsed.pop("year")
    if fields is not None:
        parsed["fields"] = fields
    if reason is None and not str(parsed.get("title") or "").strip():
        reason = "missing_title"
    return ParsedRecord(index, raw, parsed, reason)


def parse(fmt: str, data: bytes) -> list[ParsedRecord]:
    if fmt not in FORMATS:
        raise ImportFormatError("unsupported_format")
    if len(data) > MAX_BYTES:
        raise ImportFormatError("file_too_large", 413)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportFormatError("unsupported_encoding") from exc
    chunker = {"ris": _ris_chunks, "nbib": _nbib_chunks, "csv": _csv_chunks}[fmt]
    records: list[ParsedRecord] = []
    for chunk in chunker(text):
        if len(records) >= MAX_RECORDS:
            raise ImportFormatError("too_many_records", 413)
        records.append(_record(len(records), chunk))
    if not records:
        raise ImportFormatError("no_records")
    return records
