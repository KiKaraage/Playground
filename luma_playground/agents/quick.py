# SPDX-License-Identifier: Apache-2.0
"""One question to a connected agent, one text answer: for the small asks around a project (New project's
questions and proposal) when the only agents here are full ones like Codex on the person's own plan.

The agent starts in an empty folder of its own, with every permission request declined, so it can only
answer. No GTK imports; call it off the UI thread (starting an agent can take a while).
"""
from __future__ import annotations

import shutil
import tempfile

from . import acp
from .connections import Connection, launch_spec

ASK_TIMEOUT_S = 120


def ask_text(connection: Connection, prompt: str, *, timeout: float = ASK_TIMEOUT_S, registry=None) -> str:
    """The agent's reply to ``prompt``, as text. Raises acp.AcpError (or LaunchError) when it can't answer."""
    argv, env = launch_spec(connection, registry)
    folder = tempfile.mkdtemp(prefix="playground-ask-")
    chunks: list[str] = []

    def heard(_session_id: str, update: dict) -> None:
        if update.get("sessionUpdate") == "agent_message_chunk":
            content = update.get("content") or {}
            if isinstance(content, dict) and content.get("type") == "text":
                chunks.append(str(content.get("text") or ""))

    client = acp.AcpClient(argv, env=env, cwd=folder, handlers=acp.ProjectHandlers(folder, on_update=heard),
                           name=connection.id)
    try:
        client.start()
        session = client.new_session(folder)
        client.prompt(session, [{"type": "text", "text": prompt}], timeout=timeout)
    finally:
        client.close()
        shutil.rmtree(folder, ignore_errors=True)
    text = "".join(chunks).strip()
    if not text:
        raise acp.AcpError("The agent didn't answer.")
    return text
