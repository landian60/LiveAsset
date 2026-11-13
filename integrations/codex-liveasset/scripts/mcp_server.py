#!/usr/bin/env python3
"""MCP stdio interface. Install the version pinned by scripts/setup.sh."""
from mcp.server.fastmcp import FastMCP
from liveasset import Service, store_path

server = FastMCP("LiveAsset")


@server.tool()
async def learn_intents(trace: list[dict], episode: str = "dialogue", user_requested: bool = False) -> dict:
    """Learn only when the user explicitly asks to learn this dialogue into LiveAsset. Set user_requested only for that request. Writes local state."""
    return await Service(store_path()).learn(trace, episode, user_requested=user_requested)


@server.tool()
async def match_intents(request: str, context: dict | None = None, session_id: str | None = None,
                        task_id: str | None = None, scope: str = "", reroute: bool = False,
                        replay: bool = False, event_id: str | None = None) -> dict:
    """Select static assets and bind them to a task. Use a stable session_id and project scope; task_id explicitly resumes known work.
    An explicit task_id fixes identity; matching may update its assets but never replaces that ID.
    Report material context changes or set reroute=True for a new subtask. Omit task_id to allow a new goal in the same chat.
    replay=True with task_id routes against its saved asset without changing live bindings. Reuse event_id only for the same input.
    Chat references alone do not authorize learning or mean resumption. No asset learning happens here.
    """
    return await Service(store_path()).match(request, context, session_id=session_id, task_id=task_id, scope=scope,
                                             reroute=reroute, replay=replay, event_id=event_id)


@server.tool()
async def check_delivery(request: str, output: str, trace: list[dict] | None = None,
                         files: dict[str, str] | None = None, task_id: str | None = None, scope: str = "") -> dict:
    """Check a candidate reply and supplied text files, optionally using the task's bound asset snapshot without rematching.
    Route new context first to update the binding. Return unmet requirements and revision feedback.
    """
    return await Service(store_path()).check(request, output, trace=trace, files=files, task_id=task_id, scope=scope)


@server.tool()
def inspect_intents() -> dict:
    """Read the current asset without modifying it."""
    return Service(store_path()).asset()


if __name__ == "__main__":
    server.run(transport="stdio")
