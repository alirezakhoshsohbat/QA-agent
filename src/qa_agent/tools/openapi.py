"""OpenAPI / Swagger API-contract research source.

Loads OpenAPI 3 / Swagger 2 specs (JSON or YAML, from http(s) URLs or local
files) and exposes research tools the QA agent uses to discover endpoints and
read their full request/response contracts. This makes API test generation
deterministic: methods, paths, auth, parameters, request/response schemas,
status codes, and error contracts come straight from the spec instead of being
guessed. When OpenAPI is not configured (or a spec fails to load) the tools
return a clear notice so a run never hard-fails on connectivity.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
from langchain_core.tools import tool

from qa_agent.config import Settings, get_settings
from qa_agent.cost import CostTracker
from qa_agent.models.schemas import FileCache
from qa_agent.query_expansion import expand_query_terms
from qa_agent.retrieval import SEARCH_STOP_WORDS

try:  # PyYAML is a common transitive dependency; degrade gracefully if absent.
    import yaml

    _HAS_YAML = True
except ImportError:  # pragma: no cover - defensive
    _HAS_YAML = False

_HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options", "trace")
_METHOD_SET = {m.upper() for m in _HTTP_METHODS}


# ── Spec loading / parsing ──────────────────────────────────────────────────


def _parse_spec_text(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        data: Any = json.loads(text)
    except json.JSONDecodeError:
        if not _HAS_YAML:
            return None
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError:
            return None
    return data if isinstance(data, dict) else None


def _spec_name(location: str, spec: dict[str, Any]) -> str:
    info = spec.get("info") or {}
    title = str(info.get("title") or "").strip()
    if title:
        return title
    name = Path(location.split("?")[0].rstrip("/")).name
    return name or location


class OpenAPIClient:
    """Loads and caches configured OpenAPI/Swagger specs and their operations."""

    def __init__(self, settings: Settings | None = None, cost: CostTracker | None = None) -> None:
        self.settings = settings or get_settings()
        self.cost = cost
        self.cache = FileCache(self.settings.cache_dir)

    @property
    def configured(self) -> bool:
        return self.settings.openapi_configured

    def _fetch_text(self, location: str) -> str | None:
        if re.match(r"^https?://", location, re.IGNORECASE):
            if self.cost:
                self.cost.record_openapi_call()
            headers = {"Accept": "application/json, application/yaml, text/yaml, */*"}
            token = (self.settings.openapi_token or "").strip()
            if token:
                headers["Authorization"] = f"Bearer {token}"
            try:
                with httpx.Client(
                    timeout=30.0, follow_redirects=True, verify=self.settings.httpx_verify
                ) as client:
                    response = client.get(location, headers=headers)
                    response.raise_for_status()
                    return response.text
            except httpx.HTTPError:
                return None
        try:
            return Path(location).expanduser().read_text(encoding="utf-8")
        except OSError:
            return None

    def load_spec(self, location: str) -> dict[str, Any] | None:
        cache_key = f"spec_{location}"
        cached = self.cache.get("openapi", cache_key, ttl_seconds=86400)
        if cached is not None:
            return cached
        text = self._fetch_text(location)
        if text is None:
            return None
        spec = _parse_spec_text(text)
        if spec is None:
            return None
        self.cache.set("openapi", cache_key, spec)
        return spec

    def load_operations(self) -> list[dict[str, Any]]:
        operations: list[dict[str, Any]] = []
        for location in self.settings.openapi_specs_list:
            spec = self.load_spec(location)
            if not spec:
                continue
            operations.extend(_flatten_operations(spec, _spec_name(location, spec), location))
        return operations


def _flatten_operations(
    spec: dict[str, Any], spec_name: str, location: str
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    paths = spec.get("paths") or {}
    if not isinstance(paths, dict):
        return out
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        shared_params = item.get("parameters") or []
        for method, operation in item.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            out.append(
                {
                    "spec_name": spec_name,
                    "location": location,
                    "method": method.upper(),
                    "path": str(path),
                    "operation_id": str(operation.get("operationId") or ""),
                    "summary": str(operation.get("summary") or ""),
                    "description": str(operation.get("description") or ""),
                    "tags": [str(t) for t in (operation.get("tags") or [])],
                    "_op": operation,
                    "_shared_params": shared_params,
                    "_spec": spec,
                }
            )
    return out


# ── Search / ranking ────────────────────────────────────────────────────────


def _openapi_search_terms(query: str, settings: Settings | None = None) -> list[str]:
    terms: list[str] = list(expand_query_terms(query, settings))
    tokens = re.findall(r"[^\W_]+", query.lower(), flags=re.UNICODE)
    keywords = [t for t in tokens if len(t) >= 3 and t not in SEARCH_STOP_WORDS]
    terms.extend(dict.fromkeys(keywords))
    if not terms:
        terms.append(query.strip())

    deduped: list[str] = []
    seen: set[str] = set()
    for term in terms:
        key = term.lower().strip()
        if key and key not in seen:
            seen.add(key)
            deduped.append(term.strip())
    return deduped[:8]


def _score_operation(operation: dict[str, Any], terms: list[str]) -> int:
    path_lower = operation["path"].lower()
    op_id_lower = operation["operation_id"].lower()
    haystack = " ".join(
        [
            operation["method"],
            operation["path"],
            operation["operation_id"],
            operation["summary"],
            operation["description"],
            " ".join(operation["tags"]),
        ]
    ).lower()
    score = 0
    for term in terms:
        needle = term.lower().strip()
        if not needle:
            continue
        if needle in haystack:
            score += 3 if (needle in path_lower or needle in op_id_lower) else 2
            continue
        for token in re.findall(r"[a-z0-9]+", needle):
            if len(token) >= 3 and token in haystack:
                score += 1
    return score


def research_operations(
    client: OpenAPIClient, query: str, *, max_results: int = 12
) -> list[dict[str, Any]]:
    operations = client.load_operations()
    if not operations:
        return []
    terms = _openapi_search_terms(query, client.settings)
    scored = [(op, _score_operation(op, terms)) for op in operations]
    ranked = [op for op, score in sorted(scored, key=lambda x: x[1], reverse=True) if score > 0]
    if not ranked:
        # Nothing matched — surface a sample so the agent still sees the API.
        ranked = operations
    return ranked[:max_results]


def _match_operation(operations: list[dict[str, Any]], ref: str) -> dict[str, Any] | None:
    raw = (ref or "").strip()
    if not raw:
        return None
    low = raw.lower()

    parts = raw.split(None, 1)
    if len(parts) == 2 and parts[0].upper() in _METHOD_SET:
        method, path = parts[0].upper(), parts[1].strip()
        for operation in operations:
            if operation["method"] == method and operation["path"] == path:
                return operation
        for operation in operations:
            if operation["method"] == method and operation["path"].rstrip("/") == path.rstrip("/"):
                return operation

    for operation in operations:
        if operation["operation_id"] and operation["operation_id"].lower() == low:
            return operation

    for operation in operations:
        if operation["path"] == raw or operation["path"].rstrip("/") == raw.rstrip("/"):
            return operation
    return None


# ── Schema / $ref rendering ─────────────────────────────────────────────────


def _resolve_ref(spec: dict[str, Any], ref: str) -> Any:
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return None
    node: Any = spec
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


def _schema_type(spec: dict[str, Any], schema: Any) -> str:
    if not isinstance(schema, dict):
        return "any"
    if "$ref" in schema:
        return str(schema["$ref"]).split("/")[-1]
    if "allOf" in schema or "oneOf" in schema or "anyOf" in schema:
        return "object"
    stype = schema.get("type")
    if stype == "array":
        return f"array<{_schema_type(spec, schema.get('items') or {})}>"
    fmt = schema.get("format")
    if stype and fmt:
        return f"{stype}<{fmt}>"
    return str(stype) if stype else "object"


def _schema_fields(
    spec: dict[str, Any], schema: Any, depth: int = 0, seen: set[str] | None = None
) -> list[str]:
    if not isinstance(schema, dict) or depth > 4:
        return []
    seen = seen or set()

    if "$ref" in schema:
        ref = str(schema["$ref"])
        if ref in seen:
            return [f"(→ {ref.split('/')[-1]})"]
        resolved = _resolve_ref(spec, ref)
        return _schema_fields(spec, resolved, depth, seen | {ref}) if resolved else []

    for combiner in ("allOf", "oneOf", "anyOf"):
        if isinstance(schema.get(combiner), list):
            merged: list[str] = []
            for sub in schema[combiner]:
                merged.extend(_schema_fields(spec, sub, depth + 1, seen))
            return merged

    if schema.get("type") == "array":
        return [f"[array of {_schema_type(spec, schema.get('items') or {})}]"]

    props = schema.get("properties")
    if isinstance(props, dict):
        required = set(schema.get("required") or [])
        lines: list[str] = []
        for name, sub in props.items():
            sub = sub if isinstance(sub, dict) else {}
            req = " (required)" if name in required else ""
            enum = sub.get("enum")
            enum_s = f" enum={enum}" if enum else ""
            lines.append(f"- {name}: {_schema_type(spec, sub)}{req}{enum_s}")
        return lines

    if schema.get("type"):
        return [f"({_schema_type(spec, schema)})"]
    return []


def describe_operation(operation: dict[str, Any]) -> str:
    spec = operation["_spec"]
    raw = operation["_op"]
    lines = [f"# {operation['method']} {operation['path']}", f"Spec: {operation['spec_name']}"]
    if operation["operation_id"]:
        lines.append(f"operationId: {operation['operation_id']}")
    if operation["tags"]:
        lines.append(f"Tags: {', '.join(operation['tags'])}")
    if operation["summary"]:
        lines.append(f"\nSummary: {operation['summary']}")
    if operation["description"]:
        lines.append(f"Description: {operation['description'][:600]}")

    security = raw.get("security", spec.get("security"))
    if security:
        names: list[str] = []
        for requirement in security:
            if isinstance(requirement, dict):
                names.extend(requirement.keys())
        lines.append("\n## Auth")
        lines.append(f"Requires: {', '.join(sorted(set(names)))}" if names else "No auth required")

    resolved_params: list[dict[str, Any]] = []
    for param in list(operation["_shared_params"]) + list(raw.get("parameters") or []):
        if isinstance(param, dict) and "$ref" in param:
            param = _resolve_ref(spec, param["$ref"]) or {}
        if isinstance(param, dict) and param.get("name"):
            resolved_params.append(param)
    if resolved_params:
        lines.append("\n## Parameters")
        for param in resolved_params:
            schema = param.get("schema") or {}
            ptype = _schema_type(spec, schema) if schema else str(param.get("type") or "string")
            req = " (required)" if param.get("required") else ""
            desc = str(param.get("description") or "")[:90]
            suffix = f" — {desc}" if desc else ""
            lines.append(f"- {param['name']} [{param.get('in', '?')}]: {ptype}{req}{suffix}")

    body = raw.get("requestBody")
    if isinstance(body, dict):
        if "$ref" in body:
            body = _resolve_ref(spec, body["$ref"]) or {}
        content = body.get("content") or {}
        lines.append("\n## Request body")
        req = " (required)" if body.get("required") else ""
        for ctype, media in content.items():
            lines.append(f"Content-Type: {ctype}{req}")
            fields = _schema_fields(spec, (media or {}).get("schema") or {})
            lines.extend(fields[:40] or ["(schema unavailable)"])
            break
    else:
        body_params = [p for p in resolved_params if p.get("in") == "body"]
        if body_params:
            lines.append("\n## Request body")
            fields = _schema_fields(spec, body_params[0].get("schema") or {})
            lines.extend(fields[:40] or ["(schema unavailable)"])

    responses = raw.get("responses") or {}
    if isinstance(responses, dict) and responses:
        lines.append("\n## Responses")
        for status, response in responses.items():
            if isinstance(response, dict) and "$ref" in response:
                response = _resolve_ref(spec, response["$ref"]) or {}
            response = response if isinstance(response, dict) else {}
            desc = str(response.get("description") or "")[:90]
            lines.append(f"### {status} — {desc}" if desc else f"### {status}")
            schema = None
            content = response.get("content") or {}
            if content:
                first = next(iter(content.values()))
                schema = first.get("schema") if isinstance(first, dict) else None
            elif response.get("schema"):
                schema = response["schema"]
            if schema:
                lines.extend(_schema_fields(spec, schema)[:25])

    return "\n".join(lines)


def spec_to_markdown(client: OpenAPIClient, location: str) -> tuple[str, str] | None:
    """Render a whole spec as a corpus document (name, markdown)."""
    spec = client.load_spec(location)
    if not spec:
        return None
    name = _spec_name(location, spec)
    operations = _flatten_operations(spec, name, location)
    info = spec.get("info") or {}
    lines = [f"# API: {name}"]
    if info.get("version"):
        lines.append(f"Version: {info['version']}")
    if info.get("description"):
        lines.append(str(info["description"])[:600])
    lines.append(f"\nSource: {location}")
    lines.append(f"Endpoints: {len(operations)}\n")
    for operation in operations:
        lines.append("---\n")
        lines.append(describe_operation(operation))
        lines.append("")
    return name, "\n".join(lines)


# ── Tool factory ────────────────────────────────────────────────────────────


def create_openapi_tools(
    settings: Settings | None = None,
    cost: CostTracker | None = None,
) -> list:
    client = OpenAPIClient(settings, cost)

    @tool
    def openapi_research_bundle(query: str, max_results: int = 12) -> str:
        """Discover API endpoints relevant to a feature from the configured
        OpenAPI/Swagger specs.

        Prefer this FIRST for API-contract research. Returns a ranked table of
        endpoints (method, path, summary, tags). Read the top few via
        openapi_get_operation using the exact "METHOD /path" ref.
        """
        if not client.configured:
            return "OpenAPI is not configured — no specs to search."
        operations = research_operations(client, query, max_results=max_results)
        if not operations:
            return (
                "No endpoints found (specs may be empty or unreachable). "
                "Try broader terms or verify the spec URLs."
            )
        lines = [
            f"Found {len(operations)} candidate endpoints for query: {query}",
            "",
            "| Method | Path | Summary | Tags |",
            "|--------|------|---------|------|",
        ]
        for operation in operations:
            summary = (operation["summary"] or operation["operation_id"] or "").replace("|", "/")[:70]
            tags = ", ".join(operation["tags"])[:40]
            lines.append(f"| {operation['method']} | {operation['path']} | {summary} | {tags} |")
        lines.append("")
        lines.append("Read up to 3-5 endpoints via openapi_get_operation with the exact ref:")
        for operation in operations[:5]:
            label = operation["summary"] or operation["operation_id"] or ""
            lines.append(f"- READ NEXT: {operation['method']} {operation['path']} | {label}")
        return "\n".join(lines)

    @tool
    def openapi_get_operation(operation: str) -> str:
        """Read the full contract of one endpoint: parameters, request/response
        schemas, status codes, and auth.

        Pass the ref as "METHOD /path" (e.g. "POST /v1/login") or an operationId
        from the research bundle. Use the returned facts to write concrete API
        scenarios (validation, auth, boundaries, and every documented status code).
        """
        if not client.configured:
            return "OpenAPI is not configured."
        operations = client.load_operations()
        if not operations:
            return "No endpoints available — specs could not be loaded."
        target = _match_operation(operations, operation)
        if target is None:
            return (
                f"No endpoint matched '{operation}'. Use the exact 'METHOD /path' ref "
                "from openapi_research_bundle."
            )
        if cost:
            cost.add_source(f"openapi:{target['spec_name']}:{target['method']} {target['path']}")
        text = describe_operation(target)
        max_chars = 6000
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n... [truncated at {max_chars} chars for token budget]"
        return text

    return [openapi_research_bundle, openapi_get_operation]
