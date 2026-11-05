"""Экспериментальный формат: только явно выраженный будущий замысел, без дополнений."""
import copy
from dataclasses import replace
import json
from pathlib import Path

from iam.domain import jsonschema
from iam.providers.codex.classify import _json, classify
from iam.providers.codex.command import RESPONSE_JSON_SCHEMA, SYSTEM_PROMPT
from iam.providers.codex.provider import _proposals
from iam.providers.classify import Kind
from iam.providers.errors import ProviderFormatError

SCHEMA_PATH = "/opt/iam/typed-intention.schema.json"
SCHEMA = copy.deepcopy(RESPONSE_JSON_SCHEMA)
SCHEMA["properties"]["future_intention"] = {
    "type": ["object", "null"], "properties": {
        "rationale": {"type": "string"},
        "cites": copy.deepcopy(RESPONSE_JSON_SCHEMA["properties"]["proposals"]["items"]["properties"]["cites"]),
        "body": {"type": "object", "properties": {
            "op": {"type": "string", "enum": ["create"]},
            "intention_id": {"type": "string"}, "text": {"type": "string"},
            "criterion": {"type": "string"}, "project_id": {"type": "string"},
            "operation": {"type": "string", "enum": ["read", "write", "move"]}},
            "required": ["op", "intention_id", "text", "criterion", "project_id", "operation"],
            "additionalProperties": False}},
     "required": ["rationale", "cites", "body"], "additionalProperties": False}
SCHEMA["required"].append("future_intention")
PROMPT = SYSTEM_PROMPT + (
    " This experimental response also has future_intention, either null or an explicitly chosen "
    "future intention object with rationale, cites and body. Its body has op=create, intention_id, "
    "text, criterion, project_id and operation (read, write or move), with the same meanings and "
    "constraints as intention create in the supplied context. Choose all content yourself from "
    "available own experience. The adapter copies this object verbatim to an intention proposal. "
    "It never derives an intention from action text. Use null when you do not choose a future "
    "intention. An empty proposals array and null are valid together. No intention is required. "
    "The existing proposals format, allowed kinds, rights and action limits are unchanged."
)


def convert(data):
    """Reject incomplete objects; never infer, repair, or supply substantive fields."""
    if jsonschema.check(SCHEMA, data, Path('.')):
        raise ProviderFormatError("typed_intention_schema")
    result = {"proposals": copy.deepcopy(data["proposals"]), "explanation": data["explanation"]}
    future = data["future_intention"]
    if future is not None:
        result["proposals"].append({"kind": "intention", "rationale": future["rationale"],
                                    "cites": copy.deepcopy(future["cites"]),
                                    "body_json": json.dumps(future["body"], ensure_ascii=False)})
    # Shared proposal validation remains authoritative; generated IDs are not retained here.
    try:
        _proposals(result)
    except (ValueError, RecursionError):
        raise ProviderFormatError("typed_shared_schema") from None
    return result


def adapt_run(run):
    """Only transform a clean final answer; retain the private auth handoff for its owner."""
    events = []
    try:
        events = [_json(line) for line in run.stdout.splitlines() if line.strip()]
    except (ValueError, RecursionError):
        return run
    public = [event for event in events if not (isinstance(event, dict) and event.get("type") == "iam.auth.updated")]
    checked = classify(replace(run, stdout='\n'.join(json.dumps(event) for event in public)))
    if checked.kind is not Kind.OK:
        return run
    try:
        converted = convert(checked.data)
    except ProviderFormatError:
        # Let CodexProvider consume rotated credentials before rejecting the proposal schema.
        # The marker also rejects a legacy envelope that omitted the required nullable field.
        converted = {"typed_adapter_error": "invalid_explicit_envelope"}
    for event in events:
        if (event.get("type") == "item.completed" and event.get("item", {}).get("type") == "agent_message"):
            event["item"]["text"] = json.dumps(converted, ensure_ascii=False)
    return replace(run, stdout='\n'.join(json.dumps(event, ensure_ascii=False) for event in events) + '\n')


def prepare_transport(argv, stdin):
    """Only the format preamble and schema path change; context and credentials stay intact."""
    payload = _json(stdin)
    prefix = SYSTEM_PROMPT + "\n\n"
    if not payload["prompt"].startswith(prefix):
        raise ValueError("unexpected_system_prompt")
    payload["prompt"] = PROMPT + "\n\n" + payload["prompt"][len(prefix):]
    argv = list(argv)
    index = argv.index("--output-schema") + 1
    if argv[index] != "/opt/iam/response.schema.json":
        raise ValueError("unexpected_schema_path")
    argv[index] = SCHEMA_PATH
    return tuple(argv), json.dumps(payload, ensure_ascii=False)
