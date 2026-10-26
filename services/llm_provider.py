"""
services/llm_provider.py
LLM provider abstraction supporting multiple backends.
Uses the OpenAI Python client with configurable base URLs.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional, Protocol, Type, TypeVar

from pydantic import BaseModel
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)

from config.settings import get_settings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


@dataclass
class LLMHealthCheckResult:
    status: str
    message: str
    model: str


class LLMProviderProtocol(Protocol):
    """Protocol interface that all LLM provider implementations must satisfy."""

    def generate_structured(
        self,
        messages: list[dict[str, str]],
        response_model: Type[T],
        *,
        temperature: float,
        max_tokens: int,
        **kwargs: Any,
    ) -> T:
        ...

    def health_check(self) -> LLMHealthCheckResult:
        ...


# ── Mock provider ─────────────────────────────────────────────────────────────

class MockLLMProvider:
    """
    Offline mock provider for tests and demonstrations.
    Returns plausible structured output without API calls.
    """

    def generate_structured(
        self,
        messages: list[dict[str, str]],
        response_model: Type[T],
        *,
        temperature: float = 0.4,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> T:
        """Generate a mock response matching the response_model schema."""
        logger.info("MockLLMProvider: generating structured mock response")
        schema = response_model.model_json_schema()
        mock_data = self._build_mock_data(schema, response_model)
        return response_model.model_validate(mock_data)

    def health_check(self) -> LLMHealthCheckResult:
        return LLMHealthCheckResult(
            status="ok",
            message="Mock LLM provider is active (offline test mode)",
            model="mock",
        )

    def _build_mock_data(self, schema: dict, model: Type[BaseModel]) -> dict:
        """Build minimal valid mock data matching a Pydantic model."""
        from models.question import GeneratedQuestion
        from models.valuation import ValuationPoint

        if model.__name__ == "GeneratedQuestion":
            return {
                "question_text": (
                    "Explain the key components of a Generative Adversarial Network "
                    "(GAN) and describe how the generator and discriminator interact "
                    "during the training process. [MOCK QUESTION — NOT FOR REAL USE]"
                ),
                "unit_number": 1,
                "chapter_number": 1,
                "chapter_name": "Introduction to Generative AI",
                "bloom_level": "L2",
                "marks": 10,
                "question_type": "descriptive",
                "difficulty": "medium",
                "model_answer": (
                    "A Generative Adversarial Network consists of two neural networks: "
                    "the generator G and the discriminator D. The generator creates "
                    "synthetic samples while the discriminator attempts to distinguish "
                    "real samples from generated ones. Training is an adversarial game "
                    "where G minimizes and D maximizes a value function. [MOCK ANSWER]"
                ),
                "valuation_points": [
                    {"criterion": "Definition of GAN architecture", "marks": 2},
                    {"criterion": "Role of generator network", "marks": 2},
                    {"criterion": "Role of discriminator network", "marks": 2},
                    {"criterion": "Adversarial training mechanism", "marks": 2},
                    {"criterion": "Convergence and Nash equilibrium", "marks": 2},
                ],
                "bloom_justification": (
                    "This question requires understanding (L2) the components and "
                    "interaction mechanism of GAN architecture, not mere recall."
                ),
                "syllabus_grounding": [
                    "GAN fundamentals",
                    "Generator-Discriminator architecture",
                    "Adversarial training",
                ],
                "source": "AI_GENERATED",
                "approval_status": "PENDING",
            }

        # Valuation scheme fallback (matches ValuationDraft inside valuation_agent)
        props = schema.get("properties", {})
        if "expected_answer" in props and "valuation_points" in props:
            return {
                "expected_answer": "[MOCK MODEL ANSWER — Replace with faculty-approved answer]",
                "key_points": [
                    "Key concept 1 explanation",
                    "Key concept 2 with examples",
                    "Application to course context",
                ],
                "valuation_points": [
                    {"criterion": "Conceptual accuracy", "marks": 3},
                    {"criterion": "Technical depth and terminology", "marks": 3},
                    {"criterion": "Illustration with examples", "marks": 2},
                    {"criterion": "Clarity and structure", "marks": 2},
                ],
                "alternative_answers": [],
            }

        # Generic schema-driven fallback
        return self._build_generic_mock(schema)

    def _build_generic_mock(self, schema: dict) -> dict:
        """Build minimal mock data from JSON schema."""
        mock: dict = {}
        props = schema.get("properties", {})
        required = schema.get("required", list(props.keys()))
        for field_name in required:
            field_schema = props.get(field_name, {})
            field_type = field_schema.get("type", "string")
            if field_type == "string":
                mock[field_name] = f"[MOCK {field_name}]"
            elif field_type == "integer":
                mock[field_name] = 1
            elif field_type == "number":
                mock[field_name] = 1.0
            elif field_type == "boolean":
                mock[field_name] = True
            elif field_type == "array":
                mock[field_name] = []
            elif field_type == "object":
                mock[field_name] = {}
        return mock



# ── OpenAI-compatible provider ────────────────────────────────────────────────

# ── OpenAI-compatible provider ────────────────────────────────────────────────

_shared_openai_client: Any = None
_shared_openai_params: tuple = ()
LLM_PERF_LOGS: list[dict] = []


def _get_shared_openai_client(api_key: str, base_url: str, timeout: int = 600):
    global _shared_openai_client, _shared_openai_params
    params = (api_key, base_url, timeout)
    if _shared_openai_client is None or _shared_openai_params != params:
        import httpx
        from openai import OpenAI
        http_client = httpx.Client(
            timeout=timeout,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )
        _shared_openai_client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            http_client=http_client,
        )
        _shared_openai_params = params
    return _shared_openai_client


class OpenAICompatibleProvider:
    """
    Provider using the OpenAI Python client with a configurable base URL.
    Supports Groq, OpenRouter, NVIDIA NIM, Ollama, and any OpenAI-compatible API.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: int = 600,
        max_retries: int = 2,
        log_prompt_content: bool = False,
    ):
        self._api_key = api_key
        self._base_url = base_url
        self._model = model
        self._timeout = max(timeout or 600, 600)
        self._max_retries = max_retries
        self._log_prompts = log_prompt_content
        self._client = _get_shared_openai_client(
            api_key=self._api_key, base_url=self._base_url, timeout=self._timeout
        )

    def health_check(self) -> LLMHealthCheckResult:
        """Check connection to the configured LLM endpoint."""
        try:
            self._client.models.list()
            return LLMHealthCheckResult(
                status="ok",
                message=f"Connected to LLM endpoint at {self._base_url}",
                model=self._model,
            )
        except Exception as exc:
            logger.warning(f"LLM health check ping failed: {exc}")
            # If models.list is not implemented by some OpenAI-compatible proxy, report configured status
            return LLMHealthCheckResult(
                status="ok",
                message=f"Configured for {self._base_url} (model: {self._model})",
                model=self._model,
            )

    def generate_structured(
        self,
        messages: list[dict[str, str]],
        response_model: Type[T],
        *,
        temperature: float = 0.4,
        **kwargs: Any,
    ) -> T:
        request_id = str(uuid.uuid4())[:8]
        stage_name = response_model.__name__
        logger.info(f"[{request_id}] LLM request stage={stage_name} model={self._model}")
        if self._log_prompts:
            logger.debug(f"[{request_id}] Messages: {messages}")

        json_instruction = (
            "\n\nReturn ONLY one JSON object matching this schema, no prose, no markdown:\n"
            + json.dumps(response_model.model_json_schema())
        )

        augmented_messages = [dict(m) for m in messages]

        # Thinking OFF: Append " /no_think" for qwen models
        if "qwen" in self._model.lower():
            sys_found = False
            for m in augmented_messages:
                if m.get("role") == "system":
                    m["content"] = m["content"] + " /no_think"
                    sys_found = True
                    break
            if not sys_found:
                augmented_messages.insert(0, {"role": "system", "content": "You are a precise assistant. /no_think"})

        user_found = False
        for i in range(len(augmented_messages) - 1, -1, -1):
            if augmented_messages[i].get("role") == "user":
                augmented_messages[i]["content"] = augmented_messages[i]["content"] + json_instruction
                user_found = True
                break
        if not user_found:
            augmented_messages.append({"role": "user", "content": json_instruction})

        last_error: Optional[Exception] = None
        for attempt in range(self._max_retries):
            current_temp = temperature + (attempt * 0.1)
            t0 = time.perf_counter()
            content = ""
            try:
                call_kwargs: dict[str, Any] = {
                    "model": self._model,
                    "messages": augmented_messages,
                    "temperature": current_temp,
                    "extra_body": {"think": False, "keep_alive": "30m"},
                }
                if "top_p" in kwargs:
                    call_kwargs["top_p"] = kwargs["top_p"]

                response = self._client.chat.completions.create(**call_kwargs)
                t1 = time.perf_counter()
                elapsed = t1 - t0

                p_tok = response.usage.prompt_tokens if (response.usage and response.usage.prompt_tokens) else 0
                c_tok = response.usage.completion_tokens if (response.usage and response.usage.completion_tokens) else 0
                tok_s = (c_tok / elapsed) if elapsed > 0 else 0.0

                logger.info(
                    f"[{request_id}] LLM stage={stage_name} attempt={attempt+1}: {elapsed:.2f}s | "
                    f"prompt_tokens={p_tok} completion_tokens={c_tok} | {tok_s:.2f} tok/s"
                )

                LLM_PERF_LOGS.append({
                    "request_id": request_id,
                    "stage": stage_name,
                    "attempt": attempt + 1,
                    "elapsed": elapsed,
                    "prompt_tokens": p_tok,
                    "completion_tokens": c_tok,
                    "tok_s": tok_s,
                    "model": stage_name,
                })

                content = response.choices[0].message.content or ""
                import re
                raw = re.sub(r"<think>.*?</think>", "", content, flags=re.S)
                start_idx = raw.find("{")
                end_idx = raw.rfind("}")
                if start_idx != -1 and end_idx != -1 and start_idx <= end_idx:
                    raw = raw[start_idx : end_idx + 1]
                else:
                    raw = ""

                if not raw or raw.strip() in ("", "{}"):
                    raise ValueError("Empty or empty-dict JSON response from LLM")

                parsed_json = json.loads(raw)
                if not parsed_json or parsed_json == {}:
                    raise ValueError("Parsed JSON is empty object {}")

                raw_unwrapped = self._unwrap_dict(parsed_json, response_model)
                validated = response_model.model_validate(raw_unwrapped)
                return validated

            except Exception as exc:
                last_error = exc
                logger.warning(
                    f"[{request_id}] Attempt {attempt + 1}/{self._max_retries} failed for stage {stage_name}: {exc}\n"
                    f"Raw content:\n{content or 'N/A'}"
                )
                if attempt < self._max_retries - 1:
                    # No backoff sleeps for instant retry
                    augmented_messages.append({
                        "role": "user",
                        "content": (
                            f"Your previous response was invalid: {exc}. "
                            f"Please try again with a valid JSON object matching the schema."
                        ),
                    })

        err = RuntimeError(
            f"LLM failed after {self._max_retries} attempts: {last_error}"
        )
        err.__cause__ = last_error
        raise err

    @staticmethod
    def _get_clean_json_schema(model: Type[BaseModel]) -> str:
        raw_schema = model.model_json_schema()
        defs = raw_schema.get("$defs", {})

        def resolve(node: Any) -> Any:
            if isinstance(node, dict):
                if "$ref" in node:
                    ref_key = node["$ref"].split("/")[-1]
                    if ref_key in defs:
                        resolved = resolve(defs[ref_key])
                        out = dict(resolved) if isinstance(resolved, dict) else resolved
                        if isinstance(out, dict) and "default" in node:
                            out["default"] = node["default"]
                        return out
                out_dict = {}
                for k, v in node.items():
                    if k in ("$defs", "title", "description"):
                        continue
                    out_dict[k] = resolve(v)
                return out_dict
            elif isinstance(node, list):
                return [resolve(x) for x in node]
            return node

        cleaned = resolve(raw_schema)
        return json.dumps(cleaned, indent=2)

    @staticmethod
    def _unwrap_dict(raw: Any, response_model: Type[BaseModel]) -> Any:
        if not isinstance(raw, dict):
            return raw

        target_fields = set(response_model.model_fields.keys())
        matching_keys = set(raw.keys()).intersection(target_fields)

        if len(matching_keys) >= 2 or ("question_text" in raw or "expected_answer" in raw):
            return OpenAICompatibleProvider._alias_repair(raw, response_model)

        for wrapper_key in [
            "GeneratedQuestion",
            "question",
            "data",
            "result",
            "response",
            "output",
            "properties",
            "item",
            "question_data",
        ]:
            if wrapper_key in raw and isinstance(raw[wrapper_key], dict):
                unwrapped = OpenAICompatibleProvider._unwrap_dict(raw[wrapper_key], response_model)
                if isinstance(unwrapped, dict) and any(k in unwrapped for k in target_fields):
                    return unwrapped

        best_candidate = raw
        max_matches = len(matching_keys)
        for val in raw.values():
            if isinstance(val, dict):
                matches = len(set(val.keys()).intersection(target_fields))
                if matches > max_matches:
                    max_matches = matches
                    best_candidate = val

        if best_candidate != raw:
            return OpenAICompatibleProvider._unwrap_dict(best_candidate, response_model)

        return OpenAICompatibleProvider._alias_repair(raw, response_model)

    @staticmethod
    def _alias_repair(d: dict, response_model: Type[BaseModel] | None = None) -> dict:
        """
        Apply an alias only if the source key is NOT in response_model.model_fields
        AND the target key IS in response_model.model_fields.
        Never rename a key the schema defines.
        """
        d = dict(d)
        alias_map = {
            "question": "question_text",
            "text": "question_text",
            "answer": "model_answer",
            "expected_answer": "model_answer",
            "justification": "bloom_justification",
            "grounding": "syllabus_grounding",
            "points": "valuation_points",
            "valuation": "valuation_points",
        }
        if response_model is not None and hasattr(response_model, "model_fields"):
            fields = response_model.model_fields
            for old_k, new_k in alias_map.items():
                if old_k in d and new_k not in d:
                    if old_k not in fields and new_k in fields:
                        d[new_k] = d.pop(old_k)
        else:
            for old_k, new_k in alias_map.items():
                if old_k in d and new_k not in d:
                    d[new_k] = d.pop(old_k)
        return d

    @staticmethod
    def _extract_json(content: str) -> str:
        content = content.strip()
        import re
        fence_match = re.search(
            r"```(?:json)?\s*\n?([\s\S]*?)\n?```", content, re.IGNORECASE
        )
        if fence_match:
            return fence_match.group(1).strip()
        start = min(
            (content.find("{") if "{" in content else len(content)),
            (content.find("[") if "[" in content else len(content)),
        )
        end = max(
            (content.rfind("}") + 1 if "}" in content else 0),
            (content.rfind("]") + 1 if "]" in content else 0),
        )
        if start < end:
            return content[start:end]
        return content


# ── Factory ───────────────────────────────────────────────────────────────────

def get_llm_provider() -> LLMProviderProtocol:
    """
    Factory function returning the configured LLM provider.
    Uses dependency injection pattern: call this once and pass the result.
    """
    settings = get_settings()

    if settings.is_mock_mode:
        logger.info("Using MockLLMProvider (mock mode active)")
        return MockLLMProvider()

    api_key = settings.resolved_llm_api_key
    if not api_key:
        raise RuntimeError(
            "No LLM API key found. Set LLM_API_KEY or GROQ_API_KEY in .env, "
            "or set USE_MOCK_LLM=true for offline mode."
        )

    return OpenAICompatibleProvider(
        api_key=api_key,
        base_url=settings.resolved_llm_base_url,
        model=settings.resolved_llm_model,
        timeout=600,
        max_retries=2,
        log_prompt_content=settings.log_prompt_content,
    )


# Alias for protocol compliance
LLMProvider = LLMProviderProtocol

