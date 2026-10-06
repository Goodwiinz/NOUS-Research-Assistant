"""The independent GOO-317 validators reject what they must (no ``src``).

``tests/fixtures/references/ris_reader.py`` is the RIS oracle for the
serializer tests, and the vendored CSL-data schema is the CSL oracle, so
both are proven to fail on bad input before they are trusted to pass good
output.
"""

import ast
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest
from jsonschema import Draft7Validator

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/references"
GOOD = "TY  - JOUR\r\nID  - doc1\r\nAU  - A\r\nAU  - B\r\nER  - \r\n"


def ris_reader() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "ris_reader", FIXTURES / "ris_reader.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ris_reader_accepts_good_and_keeps_repeated_tag_order() -> None:
    records = ris_reader().parse(GOOD + "\r\n" + GOOD.replace("doc1", "doc2"))
    assert [r["ID"] for r in records] == [["doc1"], ["doc2"]]
    assert records[0]["AU"] == ["A", "B"]


@pytest.mark.parametrize(
    "bad",
    [
        GOOD.replace("ID  - ", "ID - "),  # one space before the hyphen
        GOOD.replace("ID  - ", "ID  -"),  # no space after the hyphen
        GOOD.replace("ER  - \r\n", ""),  # missing ER
        GOOD.replace("ID  - doc1", "ZZ  - doc1"),  # unknown tag
        GOOD.replace("TY  - JOUR", "TY  - NOPE"),  # unknown type
        "ID  - doc1\r\n" + GOOD,  # record not started by TY
        GOOD.replace("AU  - B", "AU  - "),  # empty value
    ],
)
def test_ris_reader_rejects_bad_tag_spacing_missing_er_unknown_tag(bad: str) -> None:
    reader = ris_reader()
    with pytest.raises(reader.RisError):
        reader.parse(bad)


def test_reader_imports_nothing_from_src() -> None:
    tree = ast.parse((FIXTURES / "ris_reader.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        assert all(name.split(".")[0] != "src" for name in names), names


def test_csl_schema_is_the_vendored_official_one_and_rejects_bad_records() -> None:
    schema = json.loads((FIXTURES / "csl-data.schema.json").read_text("utf-8"))
    assert schema["$id"].startswith("https://resource.citationstyles.org/schema/")
    validator = Draft7Validator(schema)
    assert not list(validator.iter_errors([{"id": "doc1", "type": "book"}]))
    for bad in (
        [{"id": "doc1"}],  # no type
        [{"id": "doc1", "type": "journal"}],  # not a CSL type
        [{"id": "doc1", "type": "book", "author": [{"name": "x"}]}],
        [{"id": "doc1", "type": "book", "abstrakt": "x"}],
    ):
        assert list(validator.iter_errors(bad)), bad
