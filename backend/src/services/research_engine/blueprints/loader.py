"""BlueprintLoader: loads and validates YAML blueprint templates."""

from pathlib import Path
from typing import Any, Dict, List

import yaml
from pydantic import ValidationError

from src.schemas.research_engine import BlueprintStepDefinition, StepType

TEMPLATES_DIR = Path(__file__).parent / "templates"

# Pre-compute valid step type values for validation.
_VALID_STEP_TYPES = {t.value for t in StepType}


class BlueprintLoader:
    """Loads, lists, and validates YAML blueprint templates."""

    def __init__(self, templates_dir: Path = TEMPLATES_DIR) -> None:
        self._templates_dir = templates_dir

    def list_templates(self) -> List[Dict[str, Any]]:
        """List all available YAML templates.

        Returns a list of dicts with slug, name, description, and step_count.
        """
        results: List[Dict[str, Any]] = []
        for path in sorted(self._templates_dir.glob("*.yaml")):
            data = self.load_template(path.stem)
            results.append(
                {
                    "slug": path.stem,
                    "name": data.get("name", path.stem),
                    "description": data.get("description", ""),
                    "step_count": len(data.get("steps", [])),
                }
            )
        return results

    def load_template(self, slug: str) -> Dict[str, Any]:
        """Load and parse a YAML template by its slug (filename stem).

        Raises FileNotFoundError if the template does not exist.
        """
        templates_dir = self._templates_dir.resolve()
        path = (templates_dir / f"{slug}.yaml").resolve()
        if path.parent != templates_dir:
            raise ValueError(f"Invalid template slug: {slug}")
        if not path.exists():
            raise FileNotFoundError(f"Blueprint template not found: {slug}")
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid template '{slug}': malformed YAML") from exc
        if not isinstance(loaded, dict):
            raise ValueError(f"invalid template '{slug}': template must be an object")
        data: Dict[str, Any] = loaded
        errors = self.validate_template(data)
        if slug == "daily_research_brief" and data.get("template_source") != slug:
            errors.append(
                "daily_research_brief template_source must be daily_research_brief"
            )
        if errors:
            raise ValueError(f"invalid template '{slug}': {'; '.join(errors)}")
        return data

    def validate_template(self, template: Dict[str, Any]) -> List[str]:
        """Validate a parsed template dict.

        Returns a list of error strings. An empty list means the template is valid.
        Checks:
          - steps is not empty
          - each step has a valid type (from StepType enum values)
          - each step has a name
        """
        errors: List[str] = []
        if not isinstance(template, dict):
            return ["Template must be an object."]
        if not isinstance(template.get("name"), str) or not template["name"].strip():
            errors.append("Template is missing required field 'name'.")
        if not isinstance(template.get("parameters", {}), dict):
            errors.append("Template parameters must be an object.")

        steps = template.get("steps", [])

        if not isinstance(steps, list) or not steps:
            errors.append("Steps must not be empty.")
            return errors

        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                errors.append(f"Step {i}: step must be an object.")
                continue
            step_type = step.get("type")
            if step_type not in _VALID_STEP_TYPES:
                errors.append(
                    f"Step {i}: invalid type '{step_type}'. "
                    f"Must be one of {sorted(_VALID_STEP_TYPES)}."
                )

            if not step.get("name"):
                errors.append(f"Step {i}: missing required field 'name'.")

            try:
                BlueprintStepDefinition.model_validate(step)
            except ValidationError as exc:
                errors.append(f"Step {i}: invalid step definition: {exc.errors()!r}")

        if template.get("template_source") == "daily_research_brief":
            errors.extend(self._validate_daily_research_brief(template))

        return errors

    @staticmethod
    def _validate_daily_research_brief(template: Dict[str, Any]) -> List[str]:
        """Enforce the server-owned v1 Daily Research Brief contract."""
        errors: List[str] = []
        parameters = template.get("parameters")
        if not isinstance(parameters, dict):
            parameters = {}
        if template.get("contract_version") != 1:
            errors.append("daily_research_brief contract_version must be 1")
        if parameters.get("providers") != [
            "openalex",
            "crossref",
        ]:
            errors.append("daily_research_brief providers must use the v1 defaults")
        if parameters.get("limit_per_provider") != 25:
            errors.append("daily_research_brief limit_per_provider must default to 25")
        if template.get("constraints") != {
            "providers": {"min": 1, "max": 4},
            "limit_per_provider": {"min": 1, "max": 50},
        }:
            errors.append("daily_research_brief constraints must match the v1 bounds")
        if template.get("coverage") != {"exhaustive": False}:
            errors.append("daily_research_brief coverage.exhaustive must be false")

        steps = template.get("steps", [])
        if [step.get("type") for step in steps if isinstance(step, dict)] != [
            "search",
            "screen",
            "extract",
            "synthesize",
            "verify",
            "export",
        ]:
            errors.append("daily_research_brief steps must match the v1 topology")
            return errors

        by_type = {step["type"]: step for step in steps}
        expected_gates = {
            "screen": "screening",
            "extract": "extraction",
            "export": "final",
        }
        for step_type, review_gate in expected_gates.items():
            parameters = by_type[step_type].get("parameters")
            if not isinstance(parameters, dict):
                parameters = {}
            if parameters.get("review_gate") != review_gate:
                errors.append(
                    f"daily_research_brief {step_type}.review_gate must be {review_gate}"
                )
        export_parameters = by_type["export"].get("parameters")
        if not isinstance(export_parameters, dict):
            export_parameters = {}
        if export_parameters.get("formats") != [
            "markdown",
            "json",
            "csv",
        ]:
            errors.append("daily_research_brief export formats must match v1")
        return errors
