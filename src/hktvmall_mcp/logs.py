"""Logging: one stderr stream (stdout belongs to the MCP protocol over stdio) and one line per tool call.

The tool line carries the tool's name, its outcome and how long it took, never its arguments or an error's text:
arguments are the person's shopping list and error text can echo them.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware
from rich.logging import RichHandler

from .build_identity import log_context

log = logging.getLogger("hktvmall.tools")


_RESERVED = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def __init__(self, original: logging.Formatter | None = None):
        super().__init__()
        self.original = original

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
        }
        if self.original is not None:
            rendered = self.original.format(record)
            try:
                structured = json.loads(rendered)
            except (TypeError, ValueError):
                structured = None
            if isinstance(structured, dict):
                payload = {**payload, **structured}
            else:
                payload["msg"] = rendered
            # Keep formatter-based redaction. Raw message, extras and exception
            # text must not be reintroduced after the original sink ran.
        else:
            payload["msg"] = record.getMessage()
            for name, value in record.__dict__.items():
                if name not in _RESERVED and not name.startswith("_"):
                    _preserve(payload, name)
                    payload[name] = value
            if record.exc_info:
                payload["exc"] = self.formatException(record.exc_info)
        fields = {"level": record.levelname, "component": record.name, **log_context()}
        for name in fields:
            _preserve(payload, name)
        payload.update(fields)
        return json.dumps(payload, ensure_ascii=False, default=repr)


def _preserve(payload: dict[str, Any], name: str) -> None:
    if name in payload:
        displaced = name + "_"
        while displaced in payload:
            displaced += "_"
        payload[displaced] = payload[name]


def _native_console_dispatch(console: Any, render: Any, hook: Any) -> Any:
    """Scope render provenance to this thread without replacing print dispatch."""
    from contextlib import contextmanager
    from threading import local

    @contextmanager
    def dispatch() -> Any:
        key = "_build_identity_render_dispatch"
        with console._lock:
            state = vars(console).get(key)
            if state is None:
                missing = object()
                state = {
                    "local": local(),
                    "users": 0,
                    "render": console.render,
                    "saved_render": vars(console).get("render", missing),
                    "missing": missing,
                    "hooks": console._render_hooks,
                }

                def current() -> Any:
                    stack = getattr(state["local"], "stack", ())
                    return stack[-1] if stack else None

                def dispatch_render(*args: Any, **kwargs: Any) -> Any:
                    context = current()
                    if context is None:
                        yield from state["render"](*args, **kwargs)
                        return
                    stack = state["local"].stack
                    stack.append(None)
                    try:
                        yield from context[0](*args, **kwargs)
                    finally:
                        stack.pop()

                class DispatchHook:
                    def __init__(self, original: Any) -> None:
                        self.original = original

                    def process_renderables(self, renderables: list[Any]) -> list[Any]:
                        context = current()
                        target = self.original
                        if context is not None:
                            target = context[1](target)
                        return target.process_renderables(renderables)

                class DispatchHooks:
                    def __getattr__(self, name: str) -> Any:
                        return getattr(state["hooks"], name)

                    def __getitem__(self, index: Any) -> Any:
                        items = state["hooks"][index]
                        if current() is None:
                            return items
                        if isinstance(index, slice):
                            return [DispatchHook(item) for item in items]
                        return DispatchHook(items)

                    def __len__(self) -> int:
                        return len(state["hooks"])

                    def __iter__(self) -> Any:
                        return iter(self[:])

                state["dispatch_render"] = dispatch_render
                state["dispatch_hooks"] = DispatchHooks()
                console.render = dispatch_render
                console._render_hooks = state["dispatch_hooks"]
                vars(console)[key] = state
            state["users"] += 1
            stack = getattr(state["local"], "stack", None)
            if stack is None:
                stack = state["local"].stack = []
            stack.append((render, hook))
        try:
            yield
        finally:
            with console._lock:
                stack.pop()
                state["users"] -= 1
                if not state["users"]:
                    if console.render is state["dispatch_render"]:
                        if state["saved_render"] is state["missing"]:
                            del console.render
                        else:
                            console.render = state["saved_render"]
                    if console._render_hooks is state["dispatch_hooks"]:
                        console._render_hooks = state["hooks"]
                    del vars(console)[key]

    return dispatch()


class _RichSink(logging.Handler):
    """Enrich only the rendering retained by Rich's traceback and console policy."""

    def __init__(self, original: RichHandler) -> None:
        super().__init__(original.level)
        self.original = original
        self.filters = list(original.filters)
        self.setFormatter(original.formatter)
        self.identity_formatter = JsonFormatter(logging.Formatter())

    def setFormatter(self, formatter: logging.Formatter | None) -> None:
        super().setFormatter(formatter)
        self.original.setFormatter(formatter)

    def emit(self, record: logging.LogRecord) -> None:
        owner = self
        original: Any = self.original
        original.acquire()
        console = original.console

        class RecordConsole:
            def __getattr__(self, name: str) -> Any:
                return getattr(console, name)

            def __setattr__(self, name: str, value: Any) -> None:
                setattr(console, name, value)

            def __delattr__(self, name: str) -> None:
                delattr(console, name)

            def print(self, *objects: Any, **kwargs: Any) -> None:
                from rich.control import Control, ControlType
                from rich.live import Live
                from rich.live_render import LiveRender
                from rich.segment import Segment

                host_renderables: set[int] = set()
                markers: dict[int, tuple[Any, bool]] = {}

                class Hook:
                    def __init__(self, original_hook: Any) -> None:
                        self.original_hook = original_hook

                    def process_renderables(self, renderables: list[Any]) -> list[Any]:
                        incoming = {id(item) for item in renderables}
                        result = self.original_hook.process_renderables(renderables)
                        if isinstance(self.original_hook, Live):
                            host_renderables.update(
                                id(item)
                                for item in result
                                if id(item) not in incoming and isinstance(item, (Control, LiveRender))
                            )
                        return result

                def marker(owned: bool) -> Any:
                    # Fresh tuple identity carries provenance through native cropping.
                    controls = [(ControlType.BELL,)]
                    token = tuple(controls)
                    markers[id(token)] = (token, owned)
                    return Segment("", None, token)

                def render(renderable: Any, options: Any = None) -> Any:
                    yield marker(id(renderable) not in host_renderables)
                    yield from console.render(renderable, options)
                    yield marker(True)

                with console:
                    # Rich owns a thread-local buffer; isolate this print from host
                    # output and restore it even if a hook or renderer raises.
                    prefix = console._buffer[:]
                    console._buffer.clear()
                    try:
                        with _native_console_dispatch(console, render, Hook):
                            console.print(*objects, **kwargs)
                        raw = console._buffer[:]
                    finally:
                        console._buffer[:] = prefix
                    groups: list[tuple[bool, list[Any]]] = []
                    owned = True
                    for segment in raw:
                        provenance = markers.get(id(segment.control))
                        if provenance is not None:
                            owned = provenance[1]
                        elif groups and groups[-1][0] == owned:
                            groups[-1][1].append(segment)
                        else:
                            groups.append((owned, [segment]))
                    for owned, segments in groups:
                        if not owned:
                            console._buffer.extend(segments)
                            continue
                        rendered = console._render_buffer(segments)
                        if not rendered:
                            continue
                        retained = logging.makeLogRecord(
                            {
                                **record.__dict__,
                                "msg": rendered.rstrip("\n"),
                                "args": (),
                                "exc_info": None,
                                "exc_text": None,
                                "stack_info": None,
                            }
                        )
                        attributed = owner.identity_formatter.format(retained)
                        try:
                            structured = json.loads(attributed)
                        except (ValueError, TypeError):
                            structured = None
                        if getattr(owner.identity_formatter, "tty", False) and not isinstance(structured, dict):
                            # These formatters declare a constant prefix
                            # for every native line.
                            native_line = (rendered.rstrip("\n").splitlines() or [""])[0]
                            attributed_line = attributed.splitlines()[0]
                            header = (
                                attributed_line[: len(attributed_line) - len(native_line)]
                                if native_line
                                else attributed_line
                            )
                            retained_segments = segments[:]
                            for index in range(len(retained_segments) - 1, -1, -1):
                                segment = retained_segments[index]
                                if segment.control:
                                    continue
                                trimmed = segment.text.rstrip("\n")
                                retained_segments[index] = Segment(trimmed, segment.style)
                                if trimmed or trimmed == segment.text:
                                    break
                            line_start = True
                            for segment in retained_segments:
                                if segment.control:
                                    console._buffer.append(segment)
                                    continue
                                for part in segment.text.splitlines(keepends=True):
                                    if line_start:
                                        console._buffer.append(Segment(header))
                                    console._buffer.append(Segment(part, segment.style))
                                    line_start = part.endswith("\n")
                            console._buffer.append(Segment.line())
                        else:
                            from rich.ansi import AnsiDecoder

                            text = AnsiDecoder().decode_line(attributed + "\n")
                            text.end = ""
                            console._buffer.extend(
                                console.render(text, console.options.update(no_wrap=True, overflow="ignore"))
                            )

        try:
            original.console = RecordConsole()
            original.emit(record)
        finally:
            original.console = console
            original.release()


def setup(*, default_level: str = "INFO") -> None:
    level = os.environ.get("LOG_LEVEL", default_level).strip().upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        level = default_level
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(logging.StreamHandler(sys.stderr))
    root.setLevel(level)
    loggers = [
        logging.getLogger(),
        *(
            item
            for name, item in logging.Logger.manager.loggerDict.copy().items()
            if isinstance(item, logging.Logger)
            and name.split(".")[0]
            in {
                "asyncio",
                "hktvmall",
                "hktvmall_mcp",
                "hktvmall_login",
                "hktvmall-login",
                "hktvmall_maintenance",
                "signin_window",
                "fastmcp",
                "mcp",
                "uvicorn",
            }
        ),
    ]
    replacements: dict[logging.Handler, logging.Handler] = {}
    fallback = [] if logging.lastResort is None else [logging.lastResort]
    for handlers in [*(logger.handlers for logger in loggers), fallback]:
        for index, existing in enumerate(handlers):
            target = replacements.get(existing, existing)
            if isinstance(target, RichHandler):
                replacements[existing] = target = _RichSink(target)
            handlers[index] = target
            if not isinstance(target, _RichSink) and not isinstance(target.formatter, JsonFormatter):
                target.setFormatter(JsonFormatter(target.formatter))
    if fallback:
        logging.lastResort = fallback[0]
    # Request/body diagnostics are not application events. Preserve the previous
    # warning floor even when owned debugging is enabled.
    for name in ("uvicorn.access", "mcp.server.lowlevel.server", "httpx", "httpx2", "httpcore", "httpcore2"):
        logger = logging.getLogger(name)
        logger.setLevel(max(logging.WARNING, logger.level))


def diagnostic(message: str) -> None:
    """Retain CLI failures on their stderr channel, separately from command results."""
    record = logging.LogRecord("hktvmall.cli", logging.ERROR, "", 0, message, (), None)
    print(JsonFormatter().format(record), file=sys.stderr)


class SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        diagnostic("Invalid command arguments; run --help for usage")
        raise SystemExit(2)


class ToolCallLog(Middleware):
    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        tool = getattr(context.message, "name", "?")
        started = time.monotonic()
        try:
            result = await call_next(context)
        except ToolError as exc:
            # The tool's own classified answer (already logged with its code by server.call): one line, no text.
            # A ToolError wrapping a crash keeps its traceback.
            if exc.__cause__ is not None:
                log.exception("%s crashed after %d ms", tool, _ms(started))
            else:
                log.log(min(exc.log_level or logging.WARNING, logging.WARNING), "%s error %d ms", tool, _ms(started))
            raise
        except Exception:
            log.exception("%s crashed after %d ms", tool, _ms(started))
            raise
        failed = bool(getattr(result, "is_error", False))
        log.log(
            logging.WARNING if failed else logging.INFO, "%s %s %d ms", tool, "error" if failed else "ok", _ms(started)
        )
        return result


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
