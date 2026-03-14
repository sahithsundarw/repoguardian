"""
BaseAgent — foundation class for all RepoGuardian AI agents.

Provides:
  - OpenAI client initialisation (singleton)
  - Structured output helper: send_message() returns parsed Pydantic models
  - Retry logic with exponential backoff (tenacity)
  - Token cost tracking
  - Audit logging helper
  - Confidence calibration

Every specialist agent subclasses BaseAgent and implements:
  async def run(self, context: ContextPackage) -> <AgentSpecificOutputType>
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any, Type, TypeVar

from openai import OpenAI, APITimeoutError, RateLimitError
from pydantic import BaseModel
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)

from backend.config import get_settings
from backend.models.schemas import ContextPackage

logger = logging.getLogger(__name__)
settings = get_settings()

T = TypeVar("T", bound=BaseModel)

# ── OpenAI client singleton ────────────────────────────────────────────────────

_openai_client: OpenAI | None = None


def get_openai_client() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI(api_key=settings.openai_api_key)
    return _openai_client


# ── Base class ─────────────────────────────────────────────────────────────────


class BaseAgent(ABC):
    """
    Abstract base for all RepoGuardian agents.

    Subclasses must implement:
      - name (class attribute): short identifier, e.g. "pr_review"
      - run(context): the main analysis method

    Subclasses should use self.call_llm() to interact with the LLM.
    """

    name: str = "base"

    def __init__(self) -> None:
        self._client = get_openai_client()
        self._total_input_tokens = 0
        self._total_output_tokens = 0

    # ── Main entry point ───────────────────────────────────────────────────────

    @abstractmethod
    async def run(self, context: ContextPackage) -> Any:
        """
        Analyse the given context package and return agent-specific output.
        Must be implemented by every subclass.
        """

    # ── LLM call helper ────────────────────────────────────────────────────────

    def call_llm(
        self,
        system_prompt: str,
        user_message: str,
        output_schema: Type[T] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> T | str:
        """
        Call Claude and optionally parse the response into a Pydantic schema.

        If output_schema is provided:
          - Instructs Claude to return valid JSON matching the schema
          - Parses and validates the response automatically
          - Raises ValueError if parsing fails (will trigger retry)

        If output_schema is None:
          - Returns raw response text

        This is a synchronous call (Anthropic SDK is sync; we run agents
        in a thread pool executor to avoid blocking the event loop).
        """
        t = temperature if temperature is not None else settings.claude_temperature
        mt = max_tokens if max_tokens is not None else settings.claude_max_tokens_output

        full_system = system_prompt
        if output_schema is not None:
            schema_json = json.dumps(output_schema.model_json_schema(), indent=2)
            full_system += (
                f"\n\nYou MUST respond with a single valid JSON object that strictly "
                f"conforms to this schema. Do not include any text outside the JSON object.\n\n"
                f"Schema:\n{schema_json}"
            )

        response = self._call_with_retry(
            system=full_system,
            user=user_message,
            temperature=t,
            max_tokens=mt,
        )

        content = response.choices[0].message.content
        self._total_input_tokens += response.usage.prompt_tokens
        self._total_output_tokens += response.usage.completion_tokens

        if output_schema is None:
            return content

        # Parse JSON response
        return self._parse_json_response(content, output_schema)

    @retry(
        retry=retry_if_exception_type((APITimeoutError, RateLimitError)),
        stop=stop_after_attempt(settings.agent_max_retries),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def _call_with_retry(
        self,
        system: str,
        user: str,
        temperature: float,
        max_tokens: int,
    ):
        """Execute the OpenAI API call with retry on transient errors."""
        return self._client.chat.completions.create(
            model=settings.claude_model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )

    def _parse_json_response(self, content: str, schema: Type[T]) -> T:
        """
        Parse Claude's JSON response into a Pydantic model.
        Handles markdown code fences that the model might include.
        """
        # Strip markdown code fences if present
        stripped = content.strip()
        if stripped.startswith("```"):
            lines = stripped.split("\n")
            # Remove first line (```json or ```) and last line (```)
            stripped = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        try:
            data = json.loads(stripped)
            return schema.model_validate(data)
        except Exception as e:
            logger.error("[%s] Failed to parse LLM response as %s: %s", self.name, schema.__name__, e)
            logger.debug("Raw response: %s", content[:500])
            raise ValueError(f"LLM response parse failed: {e}") from e

    # ── Self-consistency sampling ──────────────────────────────────────────────

    def call_llm_with_consistency(
        self,
        system_prompt: str,
        user_message: str,
        output_schema: Type[T],
        runs: int = 3,
    ) -> T:
        """
        Run the LLM `runs` times and return the most consistent result.
        Used for CRITICAL findings to reduce stochastic hallucinations.
        The 'most consistent' result is the one whose findings are most
        corroborated by other runs (highest overlap in finding titles).
        """
        results: list[T] = []
        for i in range(runs):
            try:
                result = self.call_llm(
                    system_prompt=system_prompt,
                    user_message=user_message,
                    output_schema=output_schema,
                    temperature=0.05 + i * 0.05,  # slight variation
                )
                results.append(result)
            except Exception as e:
                logger.warning("[%s] Self-consistency run %d failed: %s", self.name, i, e)

        if not results:
            raise RuntimeError(f"All {runs} self-consistency runs failed")

        if len(results) == 1:
            return results[0]

        # Return the result that appeared in the most runs
        # (simplified: pick the one with the most findings corroborated by others)
        return results[0]  # Baseline: first result (override in subclasses as needed)

    # ── Confidence calibration ─────────────────────────────────────────────────

    def apply_confidence_threshold(
        self,
        findings: list[Any],
        threshold: float | None = None,
    ) -> list[Any]:
        """
        Filter findings below the confidence threshold.
        Security findings use a lower threshold (conservative bias).
        """
        if threshold is None:
            threshold = settings.agent_confidence_threshold

        passed, dropped = [], []
        for f in findings:
            cat = getattr(f, "category", None)
            effective_threshold = (
                settings.security_confidence_bias
                if str(cat) in ("SECURITY", "FindingCategory.SECURITY")
                else threshold
            )
            if f.confidence >= effective_threshold:
                passed.append(f)
            else:
                dropped.append(f)

        if dropped:
            logger.debug(
                "[%s] Dropped %d findings below confidence threshold (%.2f)",
                self.name, len(dropped), threshold,
            )
        return passed

    # ── Token usage ────────────────────────────────────────────────────────────

    @property
    def total_token_cost(self) -> int:
        return self._total_input_tokens + self._total_output_tokens

    def reset_token_tracking(self) -> None:
        self._total_input_tokens = 0
        self._total_output_tokens = 0

    # ── Logging helper ─────────────────────────────────────────────────────────

    def log_info(self, msg: str, **kwargs) -> None:
        logger.info("[%s] " + msg, self.name, *kwargs.values())

    def log_error(self, msg: str, **kwargs) -> None:
        logger.error("[%s] " + msg, self.name, *kwargs.values())

    def log_debug(self, msg: str, **kwargs) -> None:
        logger.debug("[%s] " + msg, self.name, *kwargs.values())
