"""Situated intent asset: incremental learning, task-entry matching by situation, in-reply checks.

Asset shape (one JSON file per learning history, schema v2 since 2026-10-16):
  situations[] = {id, situation, queries[], intents[]}
  intents[]    = {id, intent_explanation, requirements[], facts[]}
  facts[]      = {fact, quote, episode}

Matching reads each `situation` and its intents' `intent_explanation` fields. The situation describes the
whole group at the level of the task request, written from its `queries` and intent explanations. An
`intent_explanation` says in which request or result the user wants the intent's requirements and
why; the checker reads it to judge whether the intent bears on the current result. `requirements` are
checkable practices; `facts` are the information the user supplied (values, names, stated exceptions), or a
value the delivered result took from material the user pointed it to, each backed by an exact quote of a user
message or tool result. Each injected intent carries its explanation, requirements and facts, so the
agent reads the same applicability the checker judges; requirements and facts are what a result is checked
against.

Learning reads one step at a time (the result before the user's message, the message, the result after it):
the step either continues the situation the previous step was in or starts a new one (another task, which may
join an existing situation of the same kind), and only that situation's intents are rewritten. After the last
step one call writes each situation as the request-level intent explanation ('When the user asks for ..., the
user wants ..., because ...') from its queries and concatenated intent explanations, merging situations that
come out the same. Learning mode `messages`, an ablation, reads the same steps with the agent's results removed.
Matching selects situations, and every intent of a selected situation is injected. After
each output IntentProgress judges every injected intent again not_applicable (the request and result do not
involve it yet, or a stated exception holds), met or unmet: only unmet asks the actor to continue, and a
continuation makes progress when more items are met afterwards. The research dialogue loop matches once at
entry; the Codex/MCP adapter can call match again when the task context changes. Core model-format failures
are recorded in receipts. Callers must inspect those receipts: a failed match is not a verified empty selection.

Prompt text lives in ./prompts.py; the names are re-exported here unchanged.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from .prompts import (CAUSE, INJECTION_MODES, MAX_PRACTICES, MESSAGE_HEAD, PART_HEAD,
                      REASK_NOTE, SCOPE_NOTE, VALIDATION_FEEDBACK, WHOLE_HEAD,
                      checker_prompt, extraction_prompt, injection, match_prompt,
                      practice_prompt, situation_prompt)

SITUATED_SCHEMA = "liveasset-situated-v2"
_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}\Z")
_SITUATION_FIELDS = {"id", "situation", "queries", "intents", "practices"}
_INTENT_FIELDS = {"id", "intent_explanation", "requirements", "facts"}
QUERY_CHARS = 300  # a stored task query is the opening of the request


def _readable(text):
    """Exclude binary content decoded as text, including readable PDF headers followed by binary data."""
    return not text or (text.count("\ufffd") + text.count("\x00")) / len(text) < 0.05


def new_situated_asset(asset_id: str, owner_id: str) -> dict[str, Any]:
    if not asset_id.strip() or not owner_id.strip():
        raise ValueError("asset_id and owner_id are required")
    return {"schema_version": SITUATED_SCHEMA, "asset_id": asset_id, "owner_id": owner_id,
            "revision": 0, "situations": []}


def is_situated(asset: Any) -> bool:
    return isinstance(asset, dict) and asset.get("schema_version") == SITUATED_SCHEMA


def _strings(value: Any, *, nonempty: bool = False) -> bool:
    return isinstance(value, list) and (bool(value) or not nonempty) \
        and all(isinstance(item, str) and item.strip() for item in value)


def validate_asset(asset: Any) -> dict[str, Any]:
    """Structural check for a stored asset (seed or learned output)."""
    if not is_situated(asset) or not isinstance(asset.get("situations"), list) \
            or not isinstance(asset.get("revision"), int) \
            or not all(isinstance(asset.get(key), str) and asset[key].strip() for key in ("asset_id", "owner_id")):
        raise ValueError(f"expected a {SITUATED_SCHEMA} asset")
    # Earlier v2 assets omitted practices; normalize only the in-memory copy.
    asset = deepcopy(asset)
    for situation in asset["situations"]:
        if isinstance(situation, dict):
            situation.setdefault("practices", [])
    seen = set()
    for situation in asset["situations"]:
        if not isinstance(situation, dict) or set(situation) != _SITUATION_FIELDS \
                or not isinstance(situation["situation"], str) or not situation["situation"].strip() \
                or not _strings(situation["queries"]) or not isinstance(situation["intents"], list) \
                or not _strings(situation["practices"]) \
                or situation["id"] in seen:
            raise ValueError("stored situation is invalid")
        seen.add(situation["id"])
        for intent in situation["intents"]:
            if not isinstance(intent, dict) or set(intent) != _INTENT_FIELDS or intent["id"] in seen \
                    or not isinstance(intent["intent_explanation"], str) \
                    or not intent["intent_explanation"].strip() \
                    or not _strings(intent["requirements"], nonempty=True) \
                    or not isinstance(intent["facts"], list) \
                    or not all(isinstance(f, dict) and {"fact", "quote", "episode"} <= set(f)
                               and set(f) <= {"fact", "quote", "episode", "scope"}
                               and ("scope" not in f or f["scope"] in {"user", "task"})
                               for f in intent["facts"]):
                raise ValueError("stored intent is invalid or duplicated")
            seen.add(intent["id"])
    return asset


def _parse_json(text: str) -> Any:
    value = text.strip()
    if value.startswith("```") and value.endswith("```"):
        value = value.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(value)


async def _ask_json(model: Any, prompt: str, accept: Any, record: dict[str, Any], *, attempts: int,
                    note: str = "") -> Any:
    """Ask until `accept` takes the parsed reply; each rejection is fed back as VALIDATION FEEDBACK.
    Every raw reply goes to record["attempts"], every call to record["calls"]. None when all attempts fail."""
    for _ in range(attempts):
        record["calls"] += 1
        response = await model.chat([{"role": "user", "content": prompt}])
        try:
            value = accept(_parse_json(response.content))
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            record["attempts"].append({"raw": response.content, "error": f"{type(exc).__name__}: {exc}"})
            prompt += VALIDATION_FEEDBACK.format(error=exc, note=note)
            continue
        record["attempts"].append({"raw": response.content})
        return value
    return None


def _intents(asset: dict[str, Any]) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    return {intent["id"]: (situation, intent)
            for situation in asset["situations"] for intent in situation["intents"]}


def _slug(text: str, used: set[str], prefix: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:60].strip("-")
    if not slug or not _ID.fullmatch(slug) or slug in used:
        n = 1
        while f"{prefix}-{n}" in used:
            n += 1
        slug = f"{prefix}-{n}"
    used.add(slug)
    return slug

FILE_CHARS = 20000  # per file shown to the learner/checker; longer text is cut with a marker




def _shown_files(files: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    """Readable text files (long ones cut with a marker) and the names of binary files."""
    shown, binary = {}, []
    for name, text in files.items():
        if not isinstance(text, str) or not _readable(text):
            binary.append(name)
        else:
            shown[name] = text if len(text) <= FILE_CHARS else \
                text[:FILE_CHARS] + f"\n[... {len(text) - FILE_CHARS} more characters not shown]"
    return shown, sorted(binary)


def transcript(visible_trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Actor-visible dialogue: user turns, agent replies, tool results, and the text files each reply
    created or changed (unchanged workspace inputs and binary files are named, not repeated)."""
    rows, seen = [], {}
    for item in visible_trace:
        if not isinstance(item, dict):
            continue
        if item.get("kind") == "tool_event":
            rows.append({"role": "tool", "name": item.get("name"), "arguments": item.get("arguments", {}),
                         "result": item.get("result_summary", "")})
        elif item.get("role") in {"user", "assistant"}:
            content = item.get("content", item.get("assistant_reply"))
            row = {"role": item["role"], "content": content}
            changed = {name: text for name, text in (item.get("artifacts") or {}).items() if seen.get(name) != text}
            seen.update(changed)
            files, other = _shown_files(changed)
            if files:
                row["files"] = files
            if other:
                row["binary_files_changed"] = sorted(other)
            rows.append(row)
    return rows


def current_dialogue(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The dialogue as it stands for the user: a continuation replaces the reply before it, so of the agent
    replies after the last user message only the latest is kept (tool results stay: their effects remain)."""
    users = [i for i, row in enumerate(rows) if row["role"] == "user"]
    start = users[-1] + 1 if users else 0
    replies = [i for i in range(start, len(rows)) if rows[i]["role"] == "assistant"]
    return [row for i, row in enumerate(rows) if i not in replies[:-1]]


# ---------------------------------------------------------------- extraction

# Incremental learning: one call per user correction, over the part of the dialogue since the last update.
# Learning-input ablation (mode `messages`): the same steps with the agent's results removed.
# Reading the cause of a correction off the agent's work; left out when that work is not shown.




def compile_extraction(raw: Any, *, asset: dict[str, Any], dialogue: list[dict[str, Any]],
                       episode: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate the model's updated situation group and put it back into the asset; other groups are kept."""
    if not isinstance(raw, dict) or set(raw) != {"situation_id", "query", "intents", "changes"} \
            or not isinstance(raw["intents"], list) or not isinstance(raw["changes"], list) \
            or not isinstance(raw["situation_id"], str) or not isinstance(raw["query"], str):
        raise ValueError("extraction must contain exactly situation_id, query, intents and changes")
    user_text = [row["content"] for row in dialogue if row["role"] == "user" and isinstance(row["content"], str)]
    # Facts may also quote the material a result relied on (tool results); queries stay user-only.
    tool_text = [row["result"] for row in dialogue
                            if row["role"] == "tool" and isinstance(row.get("result"), str)]
    changes = raw["changes"]
    for change in changes:
        if not isinstance(change, dict) or change.get("op") not in {"add", "revise", "merge", "remove", "keep"} \
                or not isinstance(change.get("intent_id"), str) or not isinstance(change.get("reason"), str):
            raise ValueError("extraction change entry is invalid")
    groups = {situation["id"]: situation for situation in asset["situations"]}
    all_ids = set(_intents(asset)) | set(groups)
    target = groups.get(raw["situation_id"])
    old = {intent["id"]: intent for intent in (target["intents"] if target else [])}
    removed = {change["intent_id"] for change in changes if change["op"] == "remove"}
    merged = {intent_id: change["intent_id"] for change in changes if change["op"] == "merge"
              for intent_id in change.get("merged_from", [])}
    # Ids of other groups are taken; an id the model invents in another language is replaced.
    used = all_ids - set(old)
    intents, seen = [], set()
    for item in raw["intents"]:
        if not isinstance(item, dict) or set(item) != _INTENT_FIELDS:
            raise ValueError("extraction intent has missing or unknown fields")
        if not isinstance(item["id"], str) or not item["id"].strip():
            raise ValueError("extraction intent id is required")
        key = item["id"] if _ID.fullmatch(item["id"]) and item["id"] not in used else _slug(item["id"], used, "intent")
        if key in seen:
            raise ValueError(f"extraction intent id is duplicated: {key}")
        seen.add(key)
        used.add(key)
        explanation = item["intent_explanation"]
        if not isinstance(explanation, str) or not explanation.strip():
            raise ValueError("extraction intent intent_explanation is required")
        if not _strings(item["requirements"], nonempty=True):
            raise ValueError("extraction intent needs a nonempty requirements string list")
        lineage = [k for k in [key, *(k for k, v in merged.items() if v == key)] if k in old]
        facts = _compile_facts(item["facts"], [f for k in lineage for f in old[k]["facts"]], user_text, tool_text,
                               episode)
        _check_requirements_point_to_material(key, item["requirements"],
                                              [f["quote"] for f in facts if any(f["quote"] in t for t in tool_text)
                                               and not any(f["quote"] in t for t in user_text)])
        intents.append({"id": key, "intent_explanation": explanation.strip(),
                        "requirements": [r.strip() for r in item["requirements"]], "facts": facts})
    missing = set(old) - seen - removed - set(merged)
    if missing:
        raise ValueError(f"existing intents dropped without remove/merge: {sorted(missing)}")
    situations = [deepcopy(s) for s in asset["situations"]]
    query = raw["query"].strip()
    if target:
        index = [s["id"] for s in situations].index(target["id"])
        queries = list(target["queries"])
        if query and any(query[:80] in text for text in user_text) and query[:QUERY_CHARS] not in queries:
            queries.append(query[:QUERY_CHARS])
        if not intents:
            del situations[index]
        else:
            situations[index] = {**target, "queries": queries, "intents": intents}
    elif intents:
        if not query or not any(query[:80] in text for text in user_text):
            raise ValueError("a new situation needs `query` copied from a user message")
        key = raw["situation_id"] if _ID.fullmatch(raw["situation_id"]) and raw["situation_id"] not in used \
            else _slug(raw["situation_id"], used, "situation")
        # The situation text is written after learning (write_situations); until then the query stands in.
        situations.append({"id": key, "situation": query[:QUERY_CHARS], "queries": [query[:QUERY_CHARS]],
                           "intents": intents, "practices": []})
    revised = {**deepcopy(asset), "situations": situations}
    if revised["situations"] != asset["situations"]:
        revised["revision"] = asset["revision"] + 1
    return revised, changes


def _compile_facts(raw: Any, previous: list[dict[str, str]], user_text: list[str], tool_text: list[str],
                   episode: str) -> list[dict[str, str]]:
    """Facts must quote this dialogue's user messages or tool results, or keep the quote of a fact the
    intent already had."""
    if not isinstance(raw, list):
        raise ValueError("extraction intent facts must be a list")
    known = {f["quote"]: f["episode"] for f in previous}
    facts = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"fact", "quote"} \
                or not all(isinstance(item[k], str) and item[k].strip() for k in ("fact", "quote")):
            raise ValueError("extraction fact must be {fact, quote} strings")
        if any(item["quote"] in text for text in [*user_text, *tool_text]):
            source = episode
        elif item["quote"] in known:
            source = known[item["quote"]]
        else:
            continue  # a fact quoting text not shown in the dialogue is dropped, not fatal
        fact = {"fact": item["fact"].strip(), "quote": item["quote"], "episode": source}
        if fact not in facts:
            facts.append(fact)
    return facts


_TOKEN = re.compile(r"`([^`]+)`|([\w./-]+)")


def _identifiers(text: str) -> set[str]:
    """Code spans, and words with a digit, an underscore, a slash or an inner dot (file, field and helper names,
    dates, amounts); plain words and sentence punctuation are not identifiers."""
    found = set()
    for span, word in _TOKEN.findall(text):
        value = (span or word).strip(".,;:-/")
        if len(value) >= 4 and (span or re.search(r"[\d_/]|\w\.\w", value)):
            found.add(value)
    return found


def _check_requirements_point_to_material(key: str, requirements: list[str], tool_quotes: list[str]) -> None:
    """A requirement names a value read from one task's material by role; restating it would hold a later task
    to that material. Only identifier-like tokens are compared (code spans, names with digits, underscores,
    slashes or inner dots)."""
    values = {value for quote in tool_quotes for value in _identifiers(quote)}
    for requirement in requirements:
        found = sorted(value for value in values if value in requirement)
        if found:
            raise ValueError(f"requirement of intent {key} restates values read from this task's material {found}; "
                             "refer to them by role ('the helper the facts name') and keep the values in facts")


LEARNING_MODES = ("episode", "incremental", "messages")


async def _extract_call(model: Any, asset: dict[str, Any], dialogue: list[dict[str, Any]],
                        earlier: list[str] | None, *, episode: str, log_path: Path,
                        current_situation: str | None = None,
                        messages_only: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    """One extraction call (two retries). On failure the asset is returned unchanged. The receipt's
    `situation` is the situation this step belongs to, passed on to the next step."""
    prompt = extraction_prompt(asset, dialogue, earlier, current_situation, messages_only)
    receipt: dict[str, Any] = {"episode": episode, "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                               "attempts": [], "status": "failed", "calls": 0}
    # Quotes may come from this part's user messages or the earlier ones it was shown.
    quotable = dialogue + [{"role": "user", "content": text} for text in earlier or []]
    done = await _ask_json(model, prompt, lambda raw: (raw, *compile_extraction(
        raw, asset=asset, dialogue=quotable, episode=episode)), receipt, attempts=3,
        note="; this feedback is not user evidence")
    if done is None:
        receipt["situation"] = current_situation
        _save(log_path, receipt)
        return deepcopy(asset), receipt
    raw, revised, changes = done
    # A new situation may get a fresh id from compile_extraction; pass on the id actually stored.
    before = {situation["id"] for situation in asset["situations"]}
    created = [situation["id"] for situation in revised["situations"] if situation["id"] not in before]
    receipt.update(situation=created[0] if created else raw["situation_id"], changes=changes,
                   status="updated" if revised["revision"] != asset["revision"] else "unchanged")
    _save(log_path, receipt)
    return revised, receipt




def compile_situations(raw: Any, asset: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {"situations"} or not isinstance(raw["situations"], list):
        raise ValueError("situation reply must contain exactly a situations list")
    groups = {situation["id"]: situation for situation in asset["situations"]}
    written, covered = [], set()
    for item in raw["situations"]:
        if not isinstance(item, dict) or set(item) != {"situation_id", "situation", "merged"} \
                or item["situation_id"] not in groups or not isinstance(item["situation"], str) \
                or not item["situation"].strip() or not isinstance(item["merged"], list):
            raise ValueError(f"situation entry is invalid: {item}")
        members = [item["situation_id"], *(key for key in item["merged"] if key != item["situation_id"])]
        if any(key not in groups or key in covered for key in members):
            raise ValueError("situation entries must cover each group exactly once")
        covered.update(members)
        written.append({"id": item["situation_id"], "situation": item["situation"].strip(),
                        "queries": [q for key in members for q in groups[key]["queries"]],
                        "intents": [i for key in members for i in groups[key]["intents"]],
                        "practices": list(dict.fromkeys(x for key in members for x in groups[key]["practices"]))})
    if covered != set(groups):
        raise ValueError(f"situation entries leave groups out: {sorted(set(groups) - covered)}")
    return {**deepcopy(asset), "situations": written, "revision": asset["revision"] + 1}


async def write_situations(model: Any, asset: dict[str, Any], *, log_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """After the last correction step: write every situation text from its queries and the concatenated
    intent explanations, merging groups that serve the same kind of request. Fails open."""
    receipt: dict[str, Any] = {"status": "empty" if not asset["situations"] else "failed", "attempts": [], "calls": 0}
    if not asset["situations"]:
        _save(log_path, receipt)
        return asset, receipt
    revised = await _ask_json(model, situation_prompt(asset), lambda raw: compile_situations(raw, asset),
                              receipt, attempts=3)
    if revised is not None:
        receipt["status"] = "written"
    _save(log_path, receipt)
    return (asset if revised is None else revised), receipt






def compile_practices(raw: Any, asset: dict[str, Any], situation_id: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {"practices"} or not _strings(raw["practices"]) \
            or len(raw["practices"]) > MAX_PRACTICES:
        raise ValueError(f"practices reply must be {{practices: at most {MAX_PRACTICES} strings}}")
    situations = deepcopy(asset["situations"])
    target = next(s for s in situations if s["id"] == situation_id)
    _check_requirements_point_to_material("practices", raw["practices"],
                                          [f["quote"] for intent in target["intents"] for f in intent["facts"]])
    target["practices"] = [x.strip() for x in raw["practices"]]
    return {**deepcopy(asset), "situations": situations, "revision": asset["revision"] + 1}


async def learn_practices(model: Any, asset: dict[str, Any], situation_id: str, dialogue: list[dict[str, Any]],
                          earlier: list[str], *, log_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """After the correction steps of a history: practices from the accepted end of its work. Fails open."""
    receipt: dict[str, Any] = {"situation": situation_id, "status": "failed", "attempts": [], "calls": 0}
    situation = next((s for s in asset["situations"] if s["id"] == situation_id), None)
    if situation is None:
        receipt["status"] = "no_situation"
        _save(log_path, receipt)
        return asset, receipt
    revised = await _ask_json(model, practice_prompt(situation, dialogue, earlier),
                              lambda raw: compile_practices(raw, asset, situation_id), receipt, attempts=3)
    if revised is not None:
        receipt["status"] = "written"
    _save(log_path, receipt)
    return (asset if revised is None else revised), receipt


def dialogue_parts(dialogue: list[dict[str, Any]]) -> list[tuple[list[dict[str, Any]], list[str]]]:
    """Incremental learning parts, one per user message after the first: the agent's result before it (from
    just after the previous user message; the request itself for the first one), the message, and the agent's
    result after it, up to the next user message. Adjacent parts share the result between them. With no
    later user message the single part is the whole dialogue. Each part comes with the user messages before it."""
    users = [i for i, row in enumerate(dialogue) if row["role"] == "user"]
    if len(users) < 2:
        return [(dialogue, [])] if dialogue else []
    parts = []
    for j in range(1, len(users)):
        start = 0 if j == 1 else users[j - 1] + 1
        end = users[j + 1] if j + 1 < len(users) else len(dialogue)
        earlier = [row["content"] for row in dialogue[:start]
                   if row["role"] == "user" and isinstance(row["content"], str)]
        parts.append((dialogue[start:end], earlier))
    return parts


async def extract(model: Any, asset: dict[str, Any], visible_trace: list[dict[str, Any]], *,
                  episode: str, log_dir: Path, mode: str = "incremental") -> tuple[dict[str, Any], dict[str, Any]]:
    """mode=incremental (default): one call per correction step (dialogue_parts), each updating the group of
    the task request it serves; a failed step leaves the asset as it was and the remaining steps still run
    (status "partial"). mode=episode: one call over the whole finished episode. mode=messages (learning-input
    ablation): the incremental steps with only their user messages. Either way one final call then writes the
    situation texts (write_situations)."""
    if mode not in LEARNING_MODES:
        raise ValueError(f"unknown learning mode: {mode}")
    dialogue = transcript(visible_trace)
    receipt: dict[str, Any] = {"episode": episode, "mode": mode, "parts": [], "calls": 0, "changes": []}
    current, situation = asset, None
    steps = [(dialogue, None)] if mode == "episode" else dialogue_parts(dialogue)
    if mode == "messages":
        steps = [([row for row in part if row["role"] == "user"], earlier) for part, earlier in steps]
    for number, (part, earlier) in enumerate(steps, 1):
        path = log_dir / ("extraction-episode.json" if mode == "episode" else f"part-{number:02}/extraction.json")
        current, step = await _extract_call(model, current, part, earlier, episode=episode, log_path=path,
                                            current_situation=situation, messages_only=mode == "messages")
        situation = step["situation"]
        receipt["calls"] += step["calls"]
        receipt["changes"] += step.get("changes", [])
        receipt["parts"].append({"part": number, "rows": len(part), "status": step["status"], "calls": step["calls"]})
    failed = [p for p in receipt["parts"] if p["status"] == "failed"]
    learned = current["revision"] != asset["revision"]
    if learned and mode != "messages" and steps:
        # Practices come from the agent's accepted work, which the messages-only ablation does not see.
        part, earlier = steps[-1]
        current, practiced = await learn_practices(model, current, situation, part, earlier,
                                                   log_path=log_dir / "practices.json")
        receipt["calls"] += practiced["calls"]
        receipt["practices"] = practiced["status"]
    if learned:
        current, written = await write_situations(model, current, log_path=log_dir / "situations.json")
        receipt["calls"] += written["calls"]
        receipt["situations"] = written["status"]
    receipt["status"] = ("failed" if receipt["parts"] and len(failed) == len(receipt["parts"]) else
                         "partial" if failed else "updated" if learned else "unchanged")
    _save(log_dir / "extraction.json", receipt)
    return current, receipt


# ------------------------------------------------------------------ matching



def compile_match(raw: Any, asset: dict[str, Any]) -> list[dict[str, str]]:
    known = {situation["id"] for situation in asset["situations"]}
    if not isinstance(raw, dict) or set(raw) != {"selected"} or not isinstance(raw["selected"], list):
        raise ValueError("match must contain exactly a selected list")
    selected, seen = [], set()
    for item in raw["selected"]:
        if not isinstance(item, dict) or set(item) != {"situation_id", "reason"} \
                or item["situation_id"] not in known or item["situation_id"] in seen \
                or not isinstance(item["reason"], str):
            raise ValueError(f"match entry is invalid: {item}")
        seen.add(item["situation_id"])
        selected.append(item)
    return selected


def compile_routed_match(raw: Any, asset: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {"relation", "selected"} or raw["relation"] not in {"continue", "new"}:
        raise ValueError("route must contain relation (continue/new) and selected")
    return {"relation": raw["relation"], "selected": compile_match({"selected": raw["selected"]}, asset)}


# full (default) injects each intent's explanation with its requirements and facts, so the agent can tell which
# requirements bear on the current work; requirements and goal (older command lines) leave the explanation out.
# Facts keep the learning task's own items and values; on another task they must not override what its request
# or material gives.




async def match(model: Any, asset: dict[str, Any], request: str, *, task_context: dict[str, Any] | None,
                log_dir: Path, all_inject: bool = False, injection_mode: str = "full",
                routing_context: dict[str, Any] | None = None, retry_empty: bool = True) -> dict[str, Any]:
    """Select applicable situations (one retry); all their intents are injected. An unusable reply selects nothing.
    An empty selection is asked once more: a single matching call can miss a situation it selects on another run."""
    result: dict[str, Any] = {"selected": [], "calls": 0, "status": "empty", "injection_mode": injection_mode}
    if not asset["situations"]:
        _save(log_dir / "match.json", result)
        return {**result, "selected_ids": [], "actor_context": None}
    if all_inject:
        result.update(status="all_inject", selected=[{"situation_id": situation["id"], "reason": "all-inject ablation"}
                                                     for situation in asset["situations"]])
    else:
        result["attempts"] = []
        prompt = match_prompt(asset, request, task_context, routing_context)
        accept = (lambda raw: compile_routed_match(raw, asset)) if routing_context is not None else (lambda raw: compile_match(raw, asset))
        answer = await _ask_json(model, prompt, accept, result, attempts=2)
        selected = answer["selected"] if routing_context is not None and answer is not None else answer
        if selected == [] and retry_empty:
            result["reasked_empty"] = True
            answer = await _ask_json(model, prompt, accept, result, attempts=2)
            selected = answer["selected"] if routing_context is not None and answer is not None else answer
        if routing_context is not None and answer is not None:
            result["relation"] = answer["relation"]
        result.update(status="failed") if selected is None else result.update(selected=selected, status="matched")
    chosen = {item["situation_id"] for item in result["selected"]}
    result["selected_ids"] = [intent["id"] for situation in asset["situations"] if situation["id"] in chosen
                              for intent in situation["intents"]]
    result["actor_context"] = injection(asset, result["selected_ids"], injection_mode)
    _save(log_dir / "match.json", result)
    return result


# ------------------------------------------------------------------ progress

class IntentProgress:
    """Per-item state for one episode over the intents injected at task entry.

    Every requirement and every user fact of an injected intent is an item (r1.., f1..). Each check is one model
    call that judges every item again, since a revision can break what an earlier output met:
    not_applicable  the current request and result do not involve it yet, or a fact the user stated as an
                    exception holds; no continuation, judged again at the next check
    met             the result fulfils it
    unmet           the result visibly misses it; the actor continues from it
    An intent is unmet when any item is, not_applicable when all are, otherwise met. An intent the checker
    leaves out is asked again once, then recorded unknown (no continuation). This instance checks a fixed
    selection; a caller that rematches creates a new instance for the updated selection.
    """

    def __init__(self, asset: dict[str, Any], selected_ids: list[str]) -> None:
        self.asset = asset
        self.known = _intents(asset)
        chosen = set(selected_ids)
        self.order = [key for key in self.known if key in chosen]
        self.nodes = {key: self.known[key][1] for key in self.order}
        self.items = {key: self._items(key) for key in self.order}
        self.state = {key: "pending" for key in self.order}
        self.item_state = {(key, item): "pending" for key in self.order for item in self.items[key]}
        self.reasons: dict[str, str] = {}
        self.item_reasons: dict[tuple[str, str], str] = {}
        self.calls = 0
        self.errors = 0

    def _items(self, key: str) -> dict[str, tuple[str, str]]:
        """r<n>: a requirement; f<n>: a fact."""
        node = self.nodes[key]
        items = {f"r{n}": ("requirement", text) for n, text in enumerate(node["requirements"], 1)}
        items.update({f"f{n}": ("fact", fact["fact"]) for n, fact in enumerate(node["facts"], 1)})
        return items

    def progress_key(self) -> tuple[int, int]:
        """A continuation makes progress when more items are met afterwards than before; every item is judged
        again at each check, so the count can also fall."""
        return sum(value == "satisfied" for value in self.item_state.values()), len(self.item_state)

    def snapshot(self) -> list[dict[str, Any]]:
        return [{"intent_id": key, "state": self.state[key], "reason": self.reasons.get(key, ""),
                 "items": [{"id": item, "state": self.item_state[(key, item)],
                            "reason": self.item_reasons.get((key, item), "")} for item in self.items[key]]}
                for key in self.order]

    def _prompt(self, keys: list[str], evidence: dict[str, Any]) -> str:
        nodes = []
        for key in keys:
            node = {"intent_id": key, "intent_explanation": self.nodes[key]["intent_explanation"],
                    "requirements": [{"id": item, "text": text} for item, (kind, text) in self.items[key].items()
                                     if kind == "requirement"]}
            facts = [{"id": item, "text": text} for item, (kind, text) in self.items[key].items() if kind == "fact"]
            nodes.append({**node, **({"facts": facts} if facts else {})})
        return checker_prompt(nodes, evidence)

    async def _ask(self, model: Any, keys: list[str], evidence: dict[str, Any], record: dict[str, Any],
                   tag: str) -> list[Any]:
        self.calls += 1
        prompt = self._prompt(keys, evidence)
        if tag == "raw_reask":
            prompt += REASK_NOTE
        response = await model.chat([{"role": "user", "content": prompt}])
        record[tag] = response.content
        try:
            parsed = _parse_json(response.content)
            if not isinstance(parsed, dict):
                raise ValueError("check reply is not an object")
            items = parsed.get("nodes", [])
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            record[tag + "_error"] = f"{type(exc).__name__}: {exc}"
            items = []
        return items if isinstance(items, list) else []

    async def _judge(self, model: Any, keys: list[str], evidence: dict[str, Any], record: dict[str, Any]) -> None:
        verdicts: dict[str, dict[str, str]] = {}
        statuses = {"not_applicable", "met", "unmet", "unknown"}

        def take(entries):
            # Missing judgments stay unknown; a retry fills gaps without replacing explicit first verdicts.
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("intent_id") not in keys:
                    continue
                key = entry["intent_id"]
                given = {item["id"]: item for item in entry.get("items") or []
                         if isinstance(item, dict) and item.get("id") in self.items[key]
                         and item.get("status") in statuses}
                if not given and entry.get("status") in statuses:
                    record.setdefault("node_level", []).append(key)
                    given = {item: {"status": entry["status"], "reason": entry.get("reason", "")}
                             for item in self.items[key]}
                if not given and self.items[key]:
                    continue
                verdicts.setdefault(key, {})
                for item, judgment in given.items():
                    if verdicts[key].get(item) in {"met", "unmet", "not_applicable"}:
                        continue
                    verdicts[key][item] = judgment["status"]
                    self.item_reasons[(key, item)] = str(judgment.get("reason", ""))

        take(await self._ask(model, keys, evidence, record, "raw"))
        def incomplete(key):
            return [item for item in self.items[key]
                    if verdicts.get(key, {}).get(item) not in {"met", "unmet", "not_applicable"}]

        omitted = [key for key in keys if key not in verdicts or incomplete(key)]
        if omitted:
            record["reasked"] = omitted
            record["reasked_items"] = {key: incomplete(key) for key in omitted}
            take(await self._ask(model, omitted, evidence, record, "raw_reask"))
        missing = [key for key in keys if key not in verdicts or incomplete(key)]
        if missing:
            self.errors += 1
            record["missing"] = missing
            record["missing_items"] = {key: incomplete(key) for key in missing}
        names = {"met": "satisfied", "unmet": "unsatisfied", "not_applicable": "not_applicable"}
        for key in keys:
            for item in self.items[key]:
                self.item_state[(key, item)] = names.get(verdicts.get(key, {}).get(item), "unknown")
                if item not in verdicts.get(key, {}):
                    self.item_reasons[(key, item)] = ""
            states = [self.item_state[(key, item)] for item in self.items[key]]
            self.state[key] = ("unknown" if key not in verdicts else
                               "unsatisfied" if "unsatisfied" in states else
                               "unknown" if "unknown" in states else
                               "not_applicable" if states and all(v == "not_applicable" for v in states) else
                               "satisfied")
            self.reasons[key] = " ".join(self.item_reasons[(key, item)] for item in self.items[key]
                                         if self.item_state[(key, item)] == "unsatisfied")

    def _todo(self) -> list[tuple[str, str]]:
        return [(key, item) for key in self.order for item in self.items[key]
                if self.item_state[(key, item)] == "unsatisfied"]

    async def check(self, model: Any, *, output: str, visible_trace: list[dict[str, Any]],
                    artifacts: dict[str, str] | None, log_path: Path,
                    task_context: dict[str, Any] | None = None) -> dict[str, Any]:
        record: dict[str, Any] = {"before": dict(self.state)}
        keys = list(self.order)
        record["checked"] = keys
        if keys:
            dialogue = current_dialogue(transcript(visible_trace))
            files, binary = _shown_files({name: base64.b64decode(value).decode("utf-8", errors="replace")
                                          for name, value in (artifacts or {}).items()})
            evidence = {"output": output, "dialogue": dialogue, "files": files,
                        "files_available": artifacts is not None, **({"binary_files": binary} if binary else {})}
            if task_context:
                evidence["task_context"] = task_context
            await self._judge(model, keys, evidence, record)
        todo = self._todo()
        result = {"complete": all(self.state[key] in {"satisfied", "not_applicable"} for key in self.order),
                  "revise": bool(todo), "nodes": self.snapshot(),
                  "not_applicable": [key for key in self.order if self.state[key] == "not_applicable"],
                  "continuation_message": self._continuation_message(todo) if todo else None}
        _save(log_path, {**record, **result})
        return result

    def _continuation_message(self, todo: list[tuple[str, str]]) -> str:
        # The actor's next message replaces its previous reply for the user, so it must stay whole.
        lines = ["LiveAsset: continue from the unmet points below; do not redo or break what is already met "
                 "(it is checked again). "
                 "Your next message replaces your previous reply, so give the complete updated result, "
                 "not only the changes. " + SCOPE_NOTE +
                 " The item text below is historical; follow the check's reason for current scope and "
                 "corrections. Fix any stated misuse without copying a value the reason says is obsolete "
                 "or belongs to another object."]
        for key, item in todo:
            kind, text = self.items[key][item]
            lines.append(f"- Not yet met: {'Known: ' if kind == 'fact' else ''}{text} "
                         f"({self.item_reasons.get((key, item), '')})")
        done = [text for key in self.order for item, (kind, text) in self.items[key].items()
                if kind == "requirement" and self.item_state[(key, item)] == "satisfied"]
        if done:
            lines.append("Already met, keep as is: " + "; ".join(done))
        return "\n".join(lines)


def continuation_cap(asset: dict[str, Any], requested: int | None) -> int:
    """Hard cap on continuations in one reply. requested=None: two per intent in the asset; the stall rule
    usually stops much earlier."""
    if requested is not None:
        return requested
    return 2 * len(_intents(asset))


def _save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
