"""Feishu approval channel over a WebSocket long connection.

Why this exists
---------------
Slack uses Socket Mode: the process dials out and Slack pushes interactions
over that connection, so no inbound reachability is required. The Feishu
channel originally used the FastAPI route ``POST /feishu/events``, which
requires Feishu's servers to reach this process — impossible behind NAT
without a public tunnel that survives restarts.

This module gives Feishu the same shape as the Slack channel: an outbound
WebSocket that receives card interactions. No public URL, no tunnel, no
domain.

Design notes
------------
* The SDK's ``lark.ws.Client`` takes over the module-level event loop in
  ``lark_oapi.ws.client``. Running it inside the already-running app loop
  raises "This event loop is already running", so it runs on a dedicated
  daemon thread with its own loop.
* ``lark.ws.Client`` exposes no ``stop()``; the connection ends when the
  process exits.
* Card actions are dispatched synchronously on that thread. The graph resume
  is async work that must run on the app loop, so it is scheduled back with
  ``asyncio.run_coroutine_threadsafe`` and never awaited before the
  acknowledgement is returned (Feishu enforces a callback deadline).
* Payload parsing is delegated to ``src.feishu_handler._resume_payload`` so
  the long-connection path and the webhook path share one implementation.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

from src.config import settings

log = logging.getLogger(__name__)

#: Event loop of the application process, set by ``register_loop`` at startup.
_app_loop: asyncio.AbstractEventLoop | None = None

_running = False
_thread: threading.Thread | None = None

#: Seconds to wait before re-dialling after the socket drops.
_RECONNECT_DELAY_SEC = 5


def register_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Record the application event loop that graph resumes must run on."""
    global _app_loop
    _app_loop = loop


def _operator_to_dict(operator: Any) -> dict[str, Any]:
    if operator is None:
        return {}
    return {
        "open_id": getattr(operator, "open_id", None) or "",
        "user_id": getattr(operator, "user_id", None) or "",
        "union_id": getattr(operator, "union_id", None) or "",
    }


def sdk_payload_to_body(event: Any) -> dict[str, Any]:
    """Convert a ``P2CardActionTrigger`` into the webhook body shape.

    ``src.feishu_handler`` parses the HTTP callback body, so reusing it means
    producing the same nested shape the route receives from Feishu.
    """
    data = getattr(event, "event", None)
    action = getattr(data, "action", None) if data is not None else None

    raw_value = getattr(action, "value", None) if action is not None else None
    if not isinstance(raw_value, dict):
        raw_value = {}

    form_value = getattr(action, "form_value", None) if action is not None else None
    if not isinstance(form_value, dict):
        form_value = {}

    action_body: dict[str, Any] = {
        "value": raw_value,
        "name": getattr(action, "name", None) or "",
        "tag": getattr(action, "tag", None) or "",
    }
    if form_value:
        action_body["form_value"] = form_value

    token = getattr(data, "token", None) if data is not None else None

    return {
        "schema": "2.0",
        "header": {"event_type": "card.action.trigger", "token": token or ""},
        "event": {
            "operator": {"operator_id": _operator_to_dict(getattr(data, "operator", None))},
            "action": action_body,
            "token": token or "",
        },
    }


def _schedule_resume(thread_id: str, resume_value: dict[str, Any]) -> None:
    """Hand the resume to the app loop from the WebSocket thread."""
    from src.graph_runner import resume as runner_resume

    loop = _app_loop
    if loop is None or loop.is_closed():
        log.error(
            "Feishu long connection: no app loop registered; resume for %s dropped",
            thread_id,
        )
        return

    async def _run() -> None:
        try:
            await runner_resume(thread_id, resume_value)
        except Exception:
            log.exception("Feishu long connection: resume failed for %s", thread_id)

    asyncio.run_coroutine_threadsafe(_run(), loop)


def _on_card_action(event: Any) -> Any:
    """Handle one card interaction; must return quickly."""
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        CallBackToast,
        P2CardActionTriggerResponse,
    )

    from src.feishu_handler import _resume_payload

    def toast(kind: str, content: str) -> Any:
        response = P2CardActionTriggerResponse()
        response.toast = CallBackToast({"type": kind, "content": content})
        return response

    try:
        body = sdk_payload_to_body(event)
        thread_id, resume_value = _resume_payload(body)
    except Exception:
        log.exception("Feishu long connection: could not parse card action")
        return toast("error", "审批数据解析失败")

    if resume_value.get("action") not in {"approve", "edit", "reject"}:
        log.warning("Feishu long connection: unrecognised action %r", resume_value)
        return toast("error", "无法识别审批操作")

    if not thread_id:
        log.warning("Feishu long connection: card action missing thread_id; resume skipped")
        return toast("error", "工单编号缺失，未执行操作")

    log.info(
        "Feishu long connection: %s for %s by %s",
        resume_value.get("action"),
        thread_id,
        resume_value.get("approver_id"),
    )
    _schedule_resume(thread_id, resume_value)
    return toast("success", "已提交，系统处理中")


def _build_ws_client() -> Any:
    import lark_oapi as lark

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_card_action_trigger(_on_card_action)
        .build()
    )
    return lark.ws.Client(
        settings.feishu_app_id,
        settings.feishu_app_secret,
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
    )


def _run_ws_forever() -> None:
    """Dial the long connection on a private loop and reconnect on failure."""
    import lark_oapi.ws.client as lark_ws_client

    previous_loop = getattr(lark_ws_client, "loop", None)
    ws_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(ws_loop)
    # The SDK reads this module-level name inside Client.start(); point it at
    # an idle loop so it does not collide with the running app loop.
    lark_ws_client.loop = ws_loop

    try:
        client = _build_ws_client()
        while _running:
            try:
                log.info("Feishu long connection: dialling")
                client.start()
            except Exception:
                log.exception("Feishu long connection: socket error")
            if _running:
                time.sleep(_RECONNECT_DELAY_SEC)
    finally:
        if getattr(lark_ws_client, "loop", None) is ws_loop:
            lark_ws_client.loop = previous_loop
        try:
            asyncio.set_event_loop(None)
        except Exception:
            log.debug("Feishu long connection: could not clear thread event loop")
        ws_loop.close()


def start() -> None:
    """Start the long connection on a daemon thread. Idempotent."""
    global _running, _thread

    if _running:
        return
    if not settings.feishu_app_id or not settings.feishu_app_secret:
        log.warning("Feishu long connection NOT started (missing app id / secret)")
        return

    _running = True
    _thread = threading.Thread(
        target=_run_ws_forever, name="feishu-ws-longconn", daemon=True
    )
    _thread.start()
    log.info("Feishu long connection started (no public URL required)")


def stop() -> None:
    """Signal the reconnect loop to exit. The SDK offers no socket stop()."""
    global _running
    _running = False


__all__ = ["register_loop", "sdk_payload_to_body", "start", "stop"]
