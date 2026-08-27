#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["agent-client-protocol==0.12.0"]
# ///
"""Run independent or persistent friend-agent sessions over ACP."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from acp import PROTOCOL_VERSION, spawn_agent_process, text_block

FRIENDS = ("claude", "agent", "devin")
SPEEDS = ("fast", "balanced", "deep")
CAPABILITIES = ("verify", "act")
CLAUDE_ACP_ADAPTER = "@agentclientprotocol/claude-agent-acp@0.65.0"

SPEED_DEFAULT_FRIEND = {"fast": "devin", "balanced": "claude", "deep": "claude"}
DEFAULT_MODELS: dict[tuple[str, str], str] = {
    ("devin", "fast"): "swe-1-7-lightning",
    ("devin", "balanced"): "claude-sonnet-5-high",
    ("devin", "deep"): "claude-opus-4-8-high",
    ("agent", "fast"): "composer-2.5[fast=true]",
    ("agent", "balanced"): "grok-4.5[effort=high,fast=false]",
    (
        "agent",
        "deep",
    ): "claude-opus-5[thinking=true,context=300k,effort=high,fast=false]",
    ("claude", "fast"): "haiku",
    ("claude", "balanced"): "default",
    ("claude", "deep"): "opus[1m]",
}
ACP_COMMANDS = {
    "claude": ("npx", "-y", CLAUDE_ACP_ADAPTER),
    "agent": ("agent", "acp"),
    "devin": ("devin", "acp"),
}
MODE_PREFERENCES = {
    ("claude", "verify"): ("plan", "dontAsk"),
    ("agent", "verify"): ("ask", "plan"),
    ("devin", "verify"): ("ask", "plan"),
    ("claude", "act"): ("bypassPermissions", "acceptEdits"),
    ("agent", "act"): ("agent",),
    ("devin", "act"): ("bypass", "accept-edits"),
}


@dataclass(frozen=True)
class Resolved:
    friend: str
    speed: str
    capability: str
    model: str
    command: tuple[str, ...]
    substituted_from: str | None = None


class FriendClient:
    """Minimal ACP client: collect answers and enforce the capability boundary."""

    def __init__(self, capability: str):
        self.capability = capability
        self._parts: dict[str, list[str]] = defaultdict(list)

    async def request_permission(
        self, session_id: str, tool_call: Any, options: list[Any], **kwargs: Any
    ) -> dict[str, Any]:
        if self.capability == "act":
            for option in options:
                if option.kind == "allow_once":
                    return {
                        "outcome": {
                            "outcome": "selected",
                            "optionId": option.option_id,
                        }
                    }
        return {"outcome": {"outcome": "cancelled"}}

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        if getattr(update, "session_update", None) != "agent_message_chunk":
            return
        content = getattr(update, "content", None)
        if getattr(content, "type", None) == "text":
            self._parts[str(session_id)].append(content.text)

    def start_turn(self, session_id: str) -> None:
        self._parts[str(session_id)] = []

    def response(self, session_id: str) -> str:
        return "".join(self._parts[str(session_id)]).strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--add-dir", action="append", default=[], type=Path)
    parser.add_argument("--prompt", action="append")
    parser.add_argument(
        "--chat",
        action="store_true",
        help="Keep one ACP session open over JSONL stdin/stdout",
    )
    parser.add_argument("--speed", choices=SPEEDS, default="balanced")
    parser.add_argument("--friend", choices=("auto", *FRIENDS), default="auto")
    parser.add_argument("--capability", choices=CAPABILITIES, default="verify")
    parser.add_argument(
        "--confirm-parallel-act",
        action="store_true",
        help="Confirm repeated act prompts have disjoint ownership and shared-state safety",
    )
    parser.add_argument("--model", help="Override the profile default ACP model value")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the resolved ACP profile only"
    )
    return parser.parse_args()


def backend_installed(friend: str) -> bool:
    return shutil.which(ACP_COMMANDS[friend][0]) is not None


def resolve(args: argparse.Namespace) -> Resolved:
    friend = SPEED_DEFAULT_FRIEND[args.speed] if args.friend == "auto" else args.friend
    substituted_from = None
    # An auto-selected backend that is not installed degrades to Claude at the same
    # speed tier rather than failing. An explicitly named --friend never substitutes:
    # the caller asked for a specific independent opinion and must be told it is absent.
    # An explicit --model is backend-specific, so it also blocks substitution.
    if (
        args.friend == "auto"
        and not args.model
        and friend != "claude"
        and not backend_installed(friend)
        and backend_installed("claude")
    ):
        substituted_from, friend = friend, "claude"
    return Resolved(
        friend=friend,
        speed=args.speed,
        capability=args.capability,
        model=args.model or DEFAULT_MODELS[(friend, args.speed)],
        command=ACP_COMMANDS[friend],
        substituted_from=substituted_from,
    )


def result_meta(index: int, resolved: Resolved) -> dict[str, Any]:
    return {
        "friend": index,
        "backend": resolved.friend,
        "speed": resolved.speed,
        "capability": resolved.capability,
        "model": resolved.model,
        **(
            {"substituted_from": resolved.substituted_from}
            if resolved.substituted_from
            else {}
        ),
    }


async def configure_session(conn: Any, session: Any, resolved: Resolved) -> str:
    available_modes = {
        mode.id for mode in (session.modes.available_modes if session.modes else [])
    }
    mode = next(
        (
            candidate
            for candidate in MODE_PREFERENCES[(resolved.friend, resolved.capability)]
            if candidate in available_modes
        ),
        None,
    )
    if mode is None:
        raise RuntimeError(
            f"ACP agent does not expose a safe {resolved.capability} mode; "
            f"available: {sorted(available_modes)}"
        )
    await conn.set_session_mode(session_id=session.session_id, mode_id=mode)

    model_option = next(
        (option for option in (session.config_options or []) if option.id == "model"),
        None,
    )
    model_values = {
        option.value for option in (getattr(model_option, "options", None) or [])
    }
    if resolved.model not in model_values:
        raise RuntimeError(
            f"ACP agent does not expose model {resolved.model!r}; "
            f"available: {sorted(model_values)}"
        )
    await conn.set_config_option(
        session_id=session.session_id,
        config_id="model",
        value=resolved.model,
    )
    return mode


async def prompt_session(
    conn: Any,
    client: FriendClient,
    session_id: str,
    prompt: str,
) -> dict[str, Any]:
    client.start_turn(session_id)
    try:
        prompt_result = await conn.prompt(
            session_id=session_id, prompt=[text_block(prompt)]
        )
    except Exception as error:
        return {"ok": False, "error": str(error)}

    response = client.response(session_id)
    if not response:
        return {"ok": False, "error": "friend returned no response"}
    result: dict[str, Any] = {
        "ok": True,
        "response": response,
        "stop_reason": prompt_result.stop_reason,
    }
    if prompt_result.usage is not None:
        result["usage"] = prompt_result.usage.model_dump(
            by_alias=True, exclude_none=True
        )
    return result


async def run_one(
    conn: Any,
    client: FriendClient,
    index: int,
    prompt: str,
    resolved: Resolved,
    args: argparse.Namespace,
) -> dict[str, Any]:
    meta = result_meta(index, resolved)
    try:
        session = await conn.new_session(
            cwd=str(args.cwd),
            additional_directories=[str(path) for path in args.add_dir],
            mcp_servers=[],
        )
        mode = await configure_session(conn, session, resolved)
        answer = await prompt_session(conn, client, session.session_id, prompt)
        return {
            **meta,
            "session_id": str(session.session_id),
            "mode": mode,
            **answer,
        }
    except Exception as error:
        return {**meta, "ok": False, "error": str(error)}


async def run_fanout(
    args: argparse.Namespace, resolved: Resolved
) -> list[dict[str, Any]]:
    client = FriendClient(resolved.capability)
    try:
        async with spawn_agent_process(
            client, *resolved.command, cwd=str(args.cwd)
        ) as (conn, _process):
            await conn.initialize(protocol_version=PROTOCOL_VERSION)
            return await asyncio.gather(
                *(
                    run_one(conn, client, index, prompt, resolved, args)
                    for index, prompt in enumerate(args.prompt, 1)
                )
            )
    except Exception as error:
        return [
            {**result_meta(index, resolved), "ok": False, "error": str(error)}
            for index, _prompt in enumerate(args.prompt, 1)
        ]


def emit(value: Any) -> None:
    print(json.dumps(value, separators=(",", ":")), flush=True)


async def run_chat(args: argparse.Namespace, resolved: Resolved) -> int:
    client = FriendClient(resolved.capability)
    async with spawn_agent_process(client, *resolved.command, cwd=str(args.cwd)) as (
        conn,
        _process,
    ):
        initialized = await conn.initialize(protocol_version=PROTOCOL_VERSION)
        session = await conn.new_session(
            cwd=str(args.cwd),
            additional_directories=[str(path) for path in args.add_dir],
            mcp_servers=[],
        )
        mode = await configure_session(conn, session, resolved)
        session_id = str(session.session_id)
        emit(
            {
                "type": "ready",
                "backend": resolved.friend,
                "agent": getattr(initialized.agent_info, "name", resolved.friend),
                "session_id": session_id,
                "mode": mode,
                "model": resolved.model,
            }
        )

        while line := await asyncio.to_thread(sys.stdin.readline):
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("expected one JSON object per line")
                if request.get("close") is True:
                    break
                prompt = request.get("prompt")
                if not isinstance(prompt, str) or not prompt.strip():
                    raise ValueError('expected {"prompt":"..."} or {"close":true}')
                answer = await prompt_session(conn, client, session_id, prompt)
                emit({"type": "response", "session_id": session_id, **answer})
            except (json.JSONDecodeError, ValueError) as error:
                emit({"type": "error", "ok": False, "error": str(error)})
    return 0


def validate_args(args: argparse.Namespace) -> str | None:
    if not args.cwd.is_dir():
        return f"cwd is not a directory: {args.cwd}"
    args.cwd = args.cwd.resolve()
    args.add_dir = [path.resolve() for path in args.add_dir]
    missing = next((path for path in args.add_dir if not path.is_dir()), None)
    if missing:
        return f"add-dir is not a directory: {missing}"
    if args.chat and args.prompt:
        return "--chat reads prompts from stdin; do not also pass --prompt"
    if not args.chat and not args.prompt and not args.dry_run:
        return "pass at least one --prompt, or use --chat"
    if (
        args.capability == "act"
        and len(args.prompt or []) > 1
        and not args.confirm_parallel_act
    ):
        return (
            "parallel act requires --confirm-parallel-act after assigning disjoint "
            "paths and eliminating shared git/index, build, and service state"
        )
    return None


def main() -> int:
    args = parse_args()
    error = validate_args(args)
    if error:
        emit({"error": error})
        return 2
    resolved = resolve(args)
    if shutil.which(resolved.command[0]) is None:
        emit(
            {
                "error": (
                    f"{resolved.command[0]} executable not found for backend "
                    f"{resolved.friend!r}; install it or drop --friend/--model so the "
                    "runner can fall back to claude"
                )
            }
        )
        return 127
    if args.dry_run:
        print(
            json.dumps(
                {
                    "transport": "acp",
                    "topology": "persistent" if args.chat else "independent-parallel",
                    "resolved": {
                        **resolved.__dict__,
                        "command": list(resolved.command),
                    },
                    "sessions": 1 if args.chat else len(args.prompt or []),
                },
                indent=2,
            )
        )
        return 0
    if args.chat:
        try:
            return asyncio.run(run_chat(args, resolved))
        except Exception as error:
            emit({"type": "error", "ok": False, "error": str(error)})
            return 1

    results = asyncio.run(run_fanout(args, resolved))
    print(json.dumps(results, indent=2))
    return 0 if all(result["ok"] for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
