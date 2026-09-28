"""Step executor for research engine workflow steps."""

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from string import Template
from typing import Any, Awaitable, Callable, Dict, List, Optional
from uuid import NAMESPACE_URL, uuid5

from src.services.research_engine.connectors.base import (
    SourceConnector,
    SourceDocument,
    redact_search_values,
)
from src.services.research_engine.discovery import (
    SEARCH_TIMEOUT_SECONDS,
    search_sources,
    source_records,
)
from src.services.research_engine.providers.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
)
from src.services.research_engine.verification import (
    QualityMark,
    run_source_grounding_check,
)

MAX_CONNECTOR_FANOUT = 4
MAX_CONNECTOR_RESULTS = 50
MAX_RENDERED_PROMPT_CHARS = 16 * 1024


def _safe_render(template_str: str, context: Dict[str, Any]) -> str:
    """Render template strings safely.

    Supports both `$variable` and `{variable}` placeholders while avoiding
    arbitrary expression evaluation.
    """
    if not template_str:
        return ""

    rendered = Template(template_str).safe_substitute(context)

    # Support legacy `{variable}` placeholders in YAML templates.
    brace_pattern = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in context:
            return str(context[key])
        return match.group(0)

    return brace_pattern.sub(repl, rendered)


@dataclass
class StepResult:
    """Result of executing a single workflow step."""

    output: Dict[str, Any]
    sources_used: List[SourceDocument] = field(default_factory=list)
    quality_marks: List[QualityMark] = field(default_factory=list)
    token_count: int = 0
    inputs_hash: Optional[str] = None
    outputs_hash: Optional[str] = None
    full_prompt: Optional[str] = None
    model_id: Optional[str] = None
    model_version: Optional[str] = None
    temperature: float = 0.0
    seed: Optional[int] = None


class StepExecutor:
    """Dispatches and executes individual workflow steps."""

    def __init__(
        self,
        connectors: Dict[str, SourceConnector],
        providers: Dict[str, LLMProvider],
        strategy_context: Optional[Dict[str, Any]] = None,
        on_search_page: Optional[
            Callable[[str, Dict[str, Any], str, str, Dict[str, Any]], Awaitable[None]]
        ] = None,
    ) -> None:
        self.connectors = connectors
        self.providers = providers
        self.strategy_context = strategy_context or {}
        self.on_search_page = on_search_page
        self._handlers = {
            "search": self._execute_search,
            "screen": self._execute_screen,
            "extract": self._execute_extract,
            "synthesize": self._execute_synthesize,
            "export": self._execute_export,
            "verify": self._execute_verify,
        }

    async def execute(self, step_def: Dict, context: Dict) -> StepResult:
        """Execute a step based on its type, dispatching to the appropriate handler."""
        step_type = step_def.get("type", "")
        handler = self._handlers.get(step_type)
        if handler is None:
            raise ValueError(f"Unknown step type: {step_type}")
        return await handler(step_def, context)

    def _get_params(self, step_def: Dict) -> Dict:
        """Get step parameters, checking both 'params' and 'parameters' keys."""
        return step_def.get("params") or step_def.get("parameters") or {}

    async def _execute_search(self, step_def: Dict, context: Dict) -> StepResult:
        """Search across configured source connectors."""
        params = self._get_params(step_def)
        sources = params.get("sources")
        if sources is None:
            sources = (
                [params["source"]]
                if params.get("source")
                else context.get("selected_sources", context.get("sources", []))
            )
        if not isinstance(sources, list) or not all(
            isinstance(name, str) for name in sources
        ):
            raise ValueError("sources must be a list of provider names")
        if len(sources) > MAX_CONNECTOR_FANOUT:
            raise ValueError(
                f"connector fanout exceeds the {MAX_CONNECTOR_FANOUT}-connector limit"
            )
        query_template = params.get("query_template", "$query")
        try:
            max_results = int(params.get("max_results", MAX_CONNECTOR_RESULTS))
        except (TypeError, ValueError) as exc:
            raise ValueError("max_results must be an integer") from exc
        if max_results < 1 or max_results > MAX_CONNECTOR_RESULTS:
            raise ValueError(
                f"connector results exceed the {MAX_CONNECTOR_RESULTS}-result limit"
            )
        query = _safe_render(query_template, context)
        if len(query) > MAX_RENDERED_PROMPT_CHARS:
            raise ValueError("rendered connector query exceeds the server limit")

        strategy = {
            "schema_version": "nous.academic.search-strategy.v1",
            "project_id": self.strategy_context.get("canonical_project_id"),
            "protocol_version_id": self.strategy_context.get("protocol_version_id"),
            "effective_plan_hash": self.strategy_context.get("effective_plan_hash"),
            "blueprint_id": self.strategy_context.get("blueprint_id"),
            "blueprint_version": self.strategy_context.get("blueprint_version"),
            "step_id": step_def.get("id"),
            "intended": {
                "selected_providers": sources,
                "parameters": redact_search_values(params),
            },
            "route_limits": {
                "max_providers": MAX_CONNECTOR_FANOUT,
                "max_results_per_provider": MAX_CONNECTOR_RESULTS,
                "requested_results_per_provider": max_results,
                "request_timeout_seconds": SEARCH_TIMEOUT_SECONDS,
            },
        }
        strategy_bytes = json.dumps(
            strategy, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        strategy_version = f"sha256:{hashlib.sha256(strategy_bytes).hexdigest()}"
        strategy["strategy_version"] = strategy_version

        step_id = str(step_def.get("id") or "search")
        run_id = self.strategy_context.get("run_id")

        async def persist_page(
            provider_name: str, execution_id: str, page: Dict[str, Any]
        ) -> None:
            if self.on_search_page is not None:
                await self.on_search_page(
                    step_id, strategy, provider_name, execution_id, page
                )

        execution_namespace = (
            f"{run_id}:{step_id}:{strategy_version}" if run_id else None
        )
        all_sources, coverage = await search_sources(
            self.connectors,
            sources,
            query,
            max_results,
            execution_namespace=execution_namespace,
            on_page_update=persist_page if self.on_search_page is not None else None,
        )
        id_namespace = (
            str(
                uuid5(
                    NAMESPACE_URL,
                    f"research-source:{run_id}:{step_id}:{strategy_version}",
                )
            )
            if run_id
            else None
        )
        records = source_records(all_sources, id_namespace=id_namespace)
        source_ids_by_provider_key: Dict[tuple[str, str], List[str]] = {}
        for record in records:
            source_id = record["source_id"]
            for snapshot in record.get("metadata", {}).get("provenance", []):
                provider = snapshot.get("connector_type")
                external_id = snapshot.get("external_id")
                if provider and external_id:
                    source_ids_by_provider_key.setdefault(
                        (provider, str(external_id)), []
                    ).append(source_id)

        for provider_name, receipt in coverage["providers"].items():
            provider_source_ids: set[str] = set()
            provider_unlinked_count = 0
            for page in receipt.get("pages", []):
                page_source_ids: set[str] = set()
                linked_record_count = 0
                for key in page.get("record_keys", []):
                    matches = source_ids_by_provider_key.get(
                        (
                            key.get("provider", provider_name),
                            str(key.get("external_id")),
                        ),
                        [],
                    )
                    if matches:
                        linked_record_count += 1
                        page_source_ids.update(matches)
                provider_source_ids.update(page_source_ids)
                parsed_count = int(page.get("response", {}).get("parsed_count", 0))
                page["imported_source_ids"] = sorted(page_source_ids)
                page["imported_count"] = len(page_source_ids)
                page["unlinked_record_count"] = max(
                    parsed_count - linked_record_count, 0
                )
                provider_unlinked_count += page["unlinked_record_count"]
            receipt["imported_source_ids"] = sorted(provider_source_ids)
            receipt["imported_count"] = len(provider_source_ids)
            receipt["unlinked_record_count"] = provider_unlinked_count
        coverage["strategy_version"] = strategy_version

        quality_marks = []
        if coverage.get("all_failed"):
            quality_marks.append(
                QualityMark(
                    check_type="provider_search",
                    passed=False,
                    details="All selected search providers failed or timed out.",
                )
            )

        return StepResult(
            output={
                "sources": [s.title for s in all_sources],
                "query": query,
                "source_records": records,
                "coverage": coverage,
                "search_strategy": strategy,
                "selected_sources": sources,
            },
            sources_used=all_sources,
            quality_marks=quality_marks,
        )

    async def _execute_screen(self, step_def: Dict, context: Dict) -> StepResult:
        return await self._execute_llm_step(step_def, context)

    async def _execute_extract(self, step_def: Dict, context: Dict) -> StepResult:
        return await self._execute_llm_step(step_def, context)

    async def _execute_synthesize(self, step_def: Dict, context: Dict) -> StepResult:
        return await self._execute_llm_step(step_def, context)

    async def _execute_export(self, step_def: Dict, context: Dict) -> StepResult:
        params = self._get_params(step_def)
        export_fields = params.get("fields")
        if isinstance(export_fields, list) and export_fields:
            exported = {
                field_name: context.get(field_name) for field_name in export_fields
            }
        else:
            exported = dict(context)

        return StepResult(
            output={
                "exported": exported,
                "format": params.get("format", "json"),
            }
        )

    async def _execute_verify(self, step_def: Dict, context: Dict) -> StepResult:
        """Verification step with deterministic source-grounding quality mark."""
        claim = str(context.get("claim") or context.get("content") or "")
        source_text = str(
            context.get("source_text")
            or context.get("evidence")
            or context.get("context")
            or ""
        )
        quality_mark = run_source_grounding_check(claim, source_text)

        return StepResult(
            output={
                "verified": quality_mark.passed,
                "check": quality_mark.check_type,
                "details": quality_mark.details,
            },
            quality_marks=[quality_mark],
        )

    async def _execute_llm_step(self, step_def: Dict, context: Dict) -> StepResult:
        """Execute a step that requires LLM completion."""
        params = self._get_params(step_def)
        model_id = step_def.get("model_id") or params.get("model_id", "")
        system_prompt_template = step_def.get("system_prompt_template") or params.get(
            "system_prompt_template", ""
        )
        temperature = float(step_def.get("temperature", params.get("temperature", 0.0)))
        seed = step_def.get("seed", params.get("seed", 42))

        provider = self.providers.get(model_id)
        if provider is None:
            raise ValueError(f"No provider found for model_id: {model_id}")

        # Retrieval audit fields identify a particular observation, not evidence
        # content. Keep them persisted, but do not perturb reproducible prompts.
        prompt_context = copy.deepcopy(context)
        coverage = prompt_context.get("coverage")
        if isinstance(coverage, dict):
            coverage.pop("retrieved_at", None)
            provider_receipts = coverage.get("providers")
            if isinstance(provider_receipts, dict):
                for receipt in provider_receipts.values():
                    if not isinstance(receipt, dict):
                        continue
                    for field in (
                        "execution_id",
                        "attempt_id",
                        "started_at",
                        "completed_at",
                        "imported_source_ids",
                    ):
                        receipt.pop(field, None)
                    pages = receipt.get("pages")
                    if not isinstance(pages, list):
                        continue
                    for page in pages:
                        if not isinstance(page, dict):
                            continue
                        page.pop("page_id", None)
                        page.pop("attempt_id", None)
                        page.pop("imported_source_ids", None)
                        request = page.get("request")
                        if isinstance(request, dict):
                            request.pop("requested_at", None)
                        response = page.get("response")
                        if isinstance(response, dict):
                            response.pop("received_at", None)
                            for attempt in response.get("request_attempts", []):
                                if isinstance(attempt, dict):
                                    attempt.pop("requested_at", None)
        for record in prompt_context.get("source_records", []):
            record.pop("source_id", None)
            for snapshot in record.get("metadata", {}).get("provenance", []):
                snapshot.pop("retrieved_at", None)
        system_prompt = _safe_render(system_prompt_template, prompt_context)
        if len(system_prompt) > MAX_RENDERED_PROMPT_CHARS:
            raise ValueError("rendered system prompt exceeds the server limit")

        prompt = str(prompt_context)
        if len(prompt.encode("utf-8")) > MAX_RENDERED_PROMPT_CHARS:
            raise ValueError("rendered LLM prompt exceeds the server limit")

        request = LLMRequest(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            seed=seed,
        )

        response: LLMResponse = await provider.complete(request)

        # full_prompt stores the exact permitted inputs needed to verify this
        # digest. Older rows may contain only the system-prompt string.
        inputs_hash = hashlib.sha256(
            (request.prompt + (request.system_prompt or "")).encode()
        ).hexdigest()
        outputs_hash = hashlib.sha256(response.content.encode()).hexdigest()

        return StepResult(
            output={"content": response.content},
            token_count=response.total_tokens,
            inputs_hash=inputs_hash,
            outputs_hash=outputs_hash,
            full_prompt=json.dumps(
                {"prompt": request.prompt, "system_prompt": request.system_prompt},
                separators=(",", ":"),
            ),
            model_id=response.model_id,
            model_version=response.model_version,
            temperature=response.temperature,
            seed=response.seed,
        )
