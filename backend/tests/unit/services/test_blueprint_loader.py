"""Tests for the BlueprintLoader service."""

import copy

import pytest
import yaml

from src.core.config import Settings
from src.schemas.research_engine import BlueprintStepDefinition
from src.services.research_engine.blueprints.loader import BlueprintLoader


@pytest.fixture(autouse=True)
def _daily_brief_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """GOO-338: the flag defaults off; these tests exercise the enabled feature."""
    monkeypatch.setattr("src.core.config.settings.DAILY_RESEARCH_BRIEF_ENABLED", True)


@pytest.fixture
def loader() -> BlueprintLoader:
    """Create a BlueprintLoader with the default templates directory."""
    return BlueprintLoader()


class TestListTemplates:
    """Tests for BlueprintLoader.list_templates."""

    def test_list_templates(self, loader: BlueprintLoader) -> None:
        """Returns at least 3 templates with correct fields."""
        templates = loader.list_templates()
        assert len(templates) >= 3

        slugs = {t["slug"] for t in templates}
        assert "systematic_literature_review" in slugs
        assert "evidence_synthesis" in slugs
        assert "data_extraction" in slugs

        for t in templates:
            assert "slug" in t
            assert "name" in t
            assert "description" in t
            assert "step_count" in t
            assert isinstance(t["step_count"], int)
            assert t["step_count"] > 0

    def test_daily_brief_is_visible_by_default_and_hidden_when_disabled(self) -> None:
        """The bundled template is on by default but retains its kill switch."""
        enabled = BlueprintLoader()
        assert "daily_research_brief" in {
            item["slug"] for item in enabled.list_templates()
        }
        assert (
            enabled.load_template("daily_research_brief")["template_source"]
            == "daily_research_brief"
        )

        disabled = BlueprintLoader(daily_research_brief_enabled=False)
        assert "daily_research_brief" not in {
            item["slug"] for item in disabled.list_templates()
        }
        with pytest.raises(FileNotFoundError):
            disabled.load_template("daily_research_brief")

    def test_daily_brief_release_setting_defaults_false_and_env_can_enable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert Settings.model_fields["DAILY_RESEARCH_BRIEF_ENABLED"].default is False

        monkeypatch.setenv("DAILY_RESEARCH_BRIEF_ENABLED", "true")
        assert Settings(_env_file=None).DAILY_RESEARCH_BRIEF_ENABLED is True

    def test_list_templates_rejects_invalid_bundled_template(self, tmp_path) -> None:
        """Invalid bundled YAML must fail before metadata reaches callers."""
        (tmp_path / "invalid.yaml").write_text(
            "name: Invalid\nsteps:\n  - type: invented\n    name: Broken\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="invalid template 'invalid'"):
            BlueprintLoader(tmp_path).list_templates()


class TestLoadTemplate:
    """Tests for BlueprintLoader.load_template."""

    def test_load_template(self, loader: BlueprintLoader) -> None:
        """Loads systematic_literature_review successfully with valid steps."""
        template = loader.load_template("systematic_literature_review")
        assert template["name"] is not None
        assert "steps" in template
        assert len(template["steps"]) == 6

    def test_load_template_returns_valid_steps(self, loader: BlueprintLoader) -> None:
        """Each step can be parsed as a BlueprintStepDefinition."""
        template = loader.load_template("systematic_literature_review")
        for step_data in template["steps"]:
            step = BlueprintStepDefinition(**step_data)
            assert step.name
            assert step.type

    def test_load_template_not_found(self, loader: BlueprintLoader) -> None:
        """Raises FileNotFoundError for missing templates."""
        with pytest.raises(FileNotFoundError):
            loader.load_template("nonexistent_template")

    @pytest.mark.parametrize(
        ("slug", "contents", "message"),
        [
            ("malformed", "name: [unterminated", "malformed YAML"),
            ("scalar", "- not\n- an\n- object\n", "template must be an object"),
        ],
    )
    def test_load_template_rejects_malformed_yaml_documents(
        self, tmp_path, slug: str, contents: str, message: str
    ) -> None:
        (tmp_path / f"{slug}.yaml").write_text(contents, encoding="utf-8")

        with pytest.raises(ValueError, match=message):
            BlueprintLoader(tmp_path).load_template(slug)

    def test_load_template_rejects_path_traversal(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="Invalid template slug"):
            BlueprintLoader(tmp_path).load_template("../outside")

    def test_daily_brief_slug_requires_matching_template_source(
        self, loader: BlueprintLoader, tmp_path
    ) -> None:
        template = copy.deepcopy(loader.load_template("daily_research_brief"))
        template["template_source"] = "custom"
        (tmp_path / "daily_research_brief.yaml").write_text(
            yaml.safe_dump(template), encoding="utf-8"
        )

        with pytest.raises(ValueError, match="template_source"):
            BlueprintLoader(tmp_path).load_template("daily_research_brief")

    def test_load_template_rejects_invalid_bundled_template(self, tmp_path) -> None:
        """Loading a bundled template validates its complete step contract."""
        (tmp_path / "invalid.yaml").write_text(
            "name: Invalid\nsteps:\n  - type: search\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="invalid template 'invalid'"):
            BlueprintLoader(tmp_path).load_template("invalid")

    def test_load_template_enforces_daily_brief_semantics(self, tmp_path) -> None:
        """A structurally valid Daily Brief cannot weaken its server bounds."""
        template = {
            "name": "Daily Research Brief",
            "template_source": "daily_research_brief",
            "contract_version": 1,
            "parameters": {
                "providers": ["openalex", "crossref"],
                "limit_per_provider": 25,
            },
            "constraints": {
                "providers": {"min": 1, "max": 5},
                "limit_per_provider": {"min": 1, "max": 50},
            },
            "coverage": {"exhaustive": False},
            "steps": [
                {"type": "search", "name": "Search"},
                {
                    "type": "screen",
                    "name": "Screen",
                    "parameters": {"review_gate": "screening"},
                },
                {
                    "type": "extract",
                    "name": "Extract",
                    "parameters": {"review_gate": "extraction"},
                },
                {"type": "synthesize", "name": "Synthesize"},
                {"type": "verify", "name": "Verify"},
                {
                    "type": "export",
                    "name": "Export",
                    "parameters": {
                        "review_gate": "final",
                        "formats": ["markdown", "json", "csv"],
                    },
                },
            ],
        }
        (tmp_path / "daily.yaml").write_text(yaml.safe_dump(template), encoding="utf-8")

        with pytest.raises(ValueError, match="daily_research_brief"):
            BlueprintLoader(tmp_path).load_template("daily")


class TestValidateTemplate:
    """Tests for BlueprintLoader.validate_template."""

    def test_validate_template(self, loader: BlueprintLoader) -> None:
        """No errors for a valid template."""
        template = loader.load_template("systematic_literature_review")
        errors = loader.validate_template(template)
        assert errors == []

    def test_validate_template_rejects_empty_steps(
        self, loader: BlueprintLoader
    ) -> None:
        """Returns errors when steps list is empty."""
        template = {"name": "Test", "description": "Test", "steps": []}
        errors = loader.validate_template(template)
        assert len(errors) > 0
        assert any("empty" in e.lower() or "steps" in e.lower() for e in errors)

    def test_validate_template_rejects_invalid_step_type(
        self, loader: BlueprintLoader
    ) -> None:
        """Returns errors when a step has an invalid type."""
        template = {
            "name": "Test",
            "description": "Test",
            "steps": [{"type": "invalid_type", "name": "Bad Step"}],
        }
        errors = loader.validate_template(template)
        assert len(errors) > 0

    def test_validate_template_rejects_missing_name(
        self, loader: BlueprintLoader
    ) -> None:
        """Returns errors when a step is missing a name."""
        template = {
            "name": "Test",
            "description": "Test",
            "steps": [{"type": "search"}],
        }
        errors = loader.validate_template(template)
        assert len(errors) > 0

    def test_validate_template_rejects_non_object_contracts(
        self, loader: BlueprintLoader
    ) -> None:
        assert loader.validate_template([]) == ["Template must be an object."]

        errors = loader.validate_template(
            {
                "name": " ",
                "parameters": [],
                "steps": ["not-a-step"],
            }
        )
        assert "Template is missing required field 'name'." in errors
        assert "Template parameters must be an object." in errors
        assert "Step 0: step must be an object." in errors

    @pytest.mark.parametrize(
        ("path", "value", "message"),
        [
            (("contract_version",), 2, "contract_version"),
            (("parameters", "providers"), ["openalex"], "providers"),
            (("parameters", "limit_per_provider"), 10, "limit_per_provider"),
            (("constraints",), {}, "constraints"),
            (("coverage",), {"exhaustive": True}, "coverage.exhaustive"),
            (("steps", 1, "parameters", "review_gate"), "wrong", "screen"),
            (("steps", 5, "parameters", "formats"), ["json"], "formats"),
        ],
    )
    def test_validate_daily_brief_rejects_contract_drift(
        self,
        loader: BlueprintLoader,
        path: tuple,
        value,
        message: str,
    ) -> None:
        template = copy.deepcopy(loader.load_template("daily_research_brief"))
        target = template
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

        assert any(message in error for error in loader.validate_template(template))

    def test_validate_daily_brief_rejects_non_object_parameters_and_topology(
        self, loader: BlueprintLoader
    ) -> None:
        template = copy.deepcopy(loader.load_template("daily_research_brief"))
        template["parameters"] = None
        errors = loader.validate_template(template)
        assert "Template parameters must be an object." in errors
        assert any("providers" in error for error in errors)

        template = copy.deepcopy(loader.load_template("daily_research_brief"))
        template["steps"] = template["steps"][:-1]
        assert any("topology" in error for error in loader.validate_template(template))
