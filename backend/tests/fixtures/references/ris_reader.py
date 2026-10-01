"""A strict RIS reader for tests (GOO-317), written from the RIS tag grammar.

Independent of the serializer: it imports nothing from ``src`` (a test
asserts that). Grammar enforced:

- every line is ``TAG  - value``: a tag of an uppercase letter then an
  uppercase letter or digit, two spaces, a hyphen and a space;
  ``ER  -`` may carry no value;
- a record starts with ``TY`` and ends with ``ER``; nothing but blank lines
  may sit between records;
- only tags from the published RIS tag list are accepted; a repeated tag
  (``AU``) keeps its order.

``parse(text)`` returns one ``{tag: [values...]}`` per record (``TY`` and
``ER`` included) and raises ``RisError`` on any violation.
"""

import re

LINE = re.compile(r"^([A-Z][A-Z0-9])  - (.*)$")
END = re.compile(r"^ER  -( ?)$")

# The RIS specification's tag list (Research Information Systems, "RIS
# Format Specifications"), the subset reference managers still read.
TAGS = frozenset(
    "TY ER ID T1 TI T2 T3 BT CT JF JO JA J1 J2 AU A1 A2 A3 A4 ED "
    "PY Y1 Y2 DA AB N1 N2 KW SN VL IS SP EP CY PB UR L1 L2 L3 L4 "
    "DO AN DB DP LA ET M1 M2 M3 C1 C2 C3 C4 C5 C6 C7 C8 RP ST OP "
    "CA NV SE".split()
)

TYPES = frozenset(
    "ABST ADVS AGGR ANCIENT ART BILL BLOG BOOK CASE CHAP CHART CLSWK COMP "
    "CONF CPAPER CTLG DATA DBASE DICT EBOOK ECHAP EDBOOK EJOUR ELEC ENCYC "
    "EQUA FIGURE GEN GOVDOC GRANT HEAR ICOMM INPR JFULL JOUR LEGAL MANSCPT "
    "MAP MGZN MPCT MULTI MUSIC NEWS PAMP PAT PCOMM RPRT SER SLIDE SOUND "
    "STAND STAT THES UNBILL UNPB VIDEO".split()
)


class RisError(ValueError):
    """The text violates the RIS grammar."""


def parse(text: str) -> list[dict[str, list[str]]]:
    records: list[dict[str, list[str]]] = []
    current: dict[str, list[str]] | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        if current is None and line.strip() == "":
            continue
        if END.match(line):
            if current is None:
                raise RisError(f"line {number}: ER outside a record")
            current["ER"] = [""]
            records.append(current)
            current = None
            continue
        match = LINE.match(line)
        if match is None:
            raise RisError(f"line {number}: not 'TAG  - value': {line!r}")
        tag, value = match.groups()
        if tag not in TAGS:
            raise RisError(f"line {number}: unknown tag {tag}")
        if current is None:
            if tag != "TY":
                raise RisError(f"line {number}: record must start with TY")
            if value not in TYPES:
                raise RisError(f"line {number}: unknown reference type {value!r}")
            current = {"TY": [value]}
            continue
        if tag == "TY":
            raise RisError(f"line {number}: TY inside a record")
        if value == "":
            raise RisError(f"line {number}: empty {tag} value")
        current.setdefault(tag, []).append(value)
    if current is not None:
        raise RisError("record not terminated by ER")
    return records
