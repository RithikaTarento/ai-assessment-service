"""Langfuse observability (opt-in, zero-cost when disabled).

Enable with LANGFUSE_ENABLED=true + LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY.
When disabled, everything is a no-op and Langfuse is never imported.
"""
from __future__ import annotations

import contextvars
import logging
import sys
from contextlib import contextmanager, nullcontext
from typing import Any, Optional

logger = logging.getLogger("assessment-api")

_client: Any = None
_enabled: bool = False
# No truncation — full prompts and outputs are preserved for Langfuse inspection.
# Set to an integer (e.g. 50_000) if you ever need to limit payload size.
_MAX_IO_CHARS: Optional[int] = None

# Prevents the auto-patch from double-counting calls wrapped with generation()
_manual_gen: contextvars.ContextVar[bool] = contextvars.ContextVar("lf_manual_gen", default=False)

# Per-request/task identity (user_id, session_id, tags) — set via set_identity()
_identity: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar("lf_identity", default=None)

# Friendly names for inner function names → Langfuse span labels (extend as needed)
_OP_NAMES: dict = {
    "generate_assessment": "assessment/generate",
    "fetch_course_data":   "assessment/fetch-content",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def set_identity(*, user_id=None, session_id=None, tags=None) -> None:
    """Attach user_id / session_id / tags to all LLM calls in the current context.

    Call once at the top of a request handler or async task.

    NOTE: contextvars do NOT propagate into FastAPI BackgroundTasks — for those,
    pass identity explicitly and call set_identity() inside the task body,
    or use trace() with explicit user_id/session_id arguments.
    """
    if not _enabled:
        return
    _identity.set({
        "user_id":    str(user_id) if user_id else None,
        "session_id": str(session_id) if session_id else None,
        "tags":       tags or [],
    })


def init() -> None:
    """Call once at FastAPI (and Worker) startup. No-op when disabled."""
    global _client, _enabled
    from .config import (
        LANGFUSE_ENABLED,
        LANGFUSE_PUBLIC_KEY,
        LANGFUSE_SECRET_KEY,
        LANGFUSE_HOST,
        LANGFUSE_SAMPLE_RATE,
    )
    if not LANGFUSE_ENABLED:
        logger.info("[tracing] Langfuse disabled — skipping init")
        return
    if not LANGFUSE_PUBLIC_KEY or not LANGFUSE_SECRET_KEY:
        logger.warning("[tracing] LANGFUSE_ENABLED=true but keys are missing — tracing disabled")
        return
    try:
        from langfuse import Langfuse
        host = LANGFUSE_HOST or "https://cloud.langfuse.com"
        _client = Langfuse(
            public_key=LANGFUSE_PUBLIC_KEY,
            secret_key=LANGFUSE_SECRET_KEY,
            host=host,
            sample_rate=LANGFUSE_SAMPLE_RATE,
        )
        _enabled = True
        logger.info(f"[tracing] Langfuse enabled (host={host}, sample_rate={LANGFUSE_SAMPLE_RATE})")
        _instrument_genai()
    except Exception as exc:
        logger.error(f"[tracing] init failed — tracing disabled: {exc}")


def shutdown() -> None:
    """Flush pending spans. Call at shutdown (after lifespan yield)."""
    if _enabled and _client is not None:
        try:
            _client.flush()
        except Exception as exc:
            logger.warning(f"[tracing] flush error: {exc}")


def is_enabled() -> bool:
    return _enabled


# ---------------------------------------------------------------------------
# Context managers
# ---------------------------------------------------------------------------

@contextmanager
def trace(*, name, user_id=None, session_id=None, tags=None, **metadata):
    """Root span for one operation; child generation spans nest under it.

    Usage (async code is fine inside the with block)::

        with tracing.trace(name="assessment:generate", user_id=uid, session_id=sid, tags=["worker"]):
            result = await generate_assessment(...)
    """
    if not _enabled or _client is None:
        yield
        return
    ident = _identity.get() or {}
    uid = user_id or ident.get("user_id")
    sid = session_id or ident.get("session_id")
    tgs = tags or ident.get("tags") or []
    meta = {k: str(v) for k, v in metadata.items() if v is not None}
    started = False
    try:
        from langfuse import propagate_attributes
        with _client.start_as_current_observation(name=name, as_type="span", metadata=meta):
            with propagate_attributes(user_id=uid, session_id=sid, tags=tgs, metadata=meta):
                started = True
                yield
    except Exception as exc:
        if started:
            raise
        logger.warning(f"[tracing] trace('{name}') setup error — continuing without trace: {exc}")
        yield


@contextmanager
def generation(*, model, operation, input=None, **metadata):
    """Optional manual generation span — the auto-patch skips it to avoid double-counting."""
    if not _enabled or _client is None:
        yield
        return
    started = False
    try:
        meta = {"operation": operation, **{k: str(v) for k, v in metadata.items() if v is not None}}
        with _client.start_as_current_observation(
            name=f"llm:{operation}",
            as_type="generation",
            model=model,
            input=_trunc(input) if input is not None else None,
            metadata=meta,
        ):
            started = True
            tok = _manual_gen.set(True)
            try:
                yield
            finally:
                _manual_gen.reset(tok)
    except Exception as exc:
        if started:
            raise
        logger.warning(f"[tracing] generation setup error — continuing without span: {exc}")
        yield


def update_generation(*, output=None, usage_details=None) -> None:
    if not _enabled or _client is None:
        return
    try:
        kw: dict = {}
        if output is not None:
            kw["output"] = _trunc(output)
        if usage_details:
            kw["usage_details"] = usage_details
        if kw:
            _client.update_current_generation(**kw)
    except Exception as exc:
        logger.debug(f"[tracing] update_generation: {exc}")


def record_gemini_usage(response, *, output=None) -> None:
    """Pull token counts from a google-genai response onto the current generation span.

    COST NOTE: Gemini bills thinking tokens at the output rate, so they are folded
    into the 'output' key for correct Langfuse cost metering. The 'thinking' key is
    kept as a display-only field that Langfuse does not price (no double-counting).
    """
    if not _enabled or _client is None:
        return
    usage = None
    try:
        um = getattr(response, "usage_metadata", None)
        if um is not None:
            g = lambda a: int(getattr(um, a, 0) or 0)
            candidates = g("candidates_token_count")
            thoughts   = g("thoughts_token_count")
            usage = {k: v for k, v in {
                "input":    g("prompt_token_count"),
                "output":   candidates + thoughts,   # thinking billed at output rate
                "thinking": thoughts,                # display-only — not priced by Langfuse
                "cached":   g("cached_content_token_count"),
                "total":    g("total_token_count"),
            }.items() if v}
        if output is None:
            output = getattr(response, "text", None)
    except Exception as exc:
        logger.debug(f"[tracing] usage parse error: {exc}")
    update_generation(output=output, usage_details=usage)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _trunc(v):
    """Return v unchanged. _MAX_IO_CHARS=None means no truncation (full content preserved)."""
    if _MAX_IO_CHARS is not None and isinstance(v, str) and len(v) > _MAX_IO_CHARS:
        return v[:_MAX_IO_CHARS] + f"… [+{len(v) - _MAX_IO_CHARS} chars]"
    return v


def _contents_to_text(contents):
    """Render a google-genai 'contents' arg to a plain string for Langfuse input."""
    try:
        if contents is None:
            return None
        if isinstance(contents, str):
            return contents
        out = []
        items = contents if isinstance(contents, (list, tuple)) else [contents]
        for it in items:
            if isinstance(it, str):
                out.append(it)
                continue
            parts = getattr(it, "parts", None) or (
                [it] if getattr(it, "text", None) else []
            )
            for p in parts:
                t = getattr(p, "text", None)
                if t:
                    out.append(t)
                elif getattr(p, "inline_data", None) or getattr(p, "file_data", None):
                    out.append("[binary/pdf]")
        return "\n".join(out) or None
    except Exception:
        return None


def _caller_operation() -> str:
    """Walk the call stack to find the first non-genai, non-tracing frame name."""
    try:
        f = sys._getframe(2)
        for _ in range(12):
            if f is None:
                break
            fn = f.f_code.co_filename
            if "google/genai" not in fn and not fn.endswith("tracing.py"):
                return _OP_NAMES.get(f.f_code.co_name, f.f_code.co_name)
            f = f.f_back
    except Exception:
        pass
    return "generate"


def _ident_cm():
    """Build a propagate_attributes context manager from the current _identity contextvar."""
    ident = _identity.get() or {}
    if ident.get("user_id") or ident.get("session_id") or ident.get("tags"):
        try:
            from langfuse import propagate_attributes
            return propagate_attributes(
                user_id=ident.get("user_id"),
                session_id=ident.get("session_id"),
                tags=ident.get("tags") or [],
            )
        except Exception:
            return nullcontext()
    return nullcontext()


def _instrument_genai() -> None:
    """Monkeypatch google-genai AsyncModels so every generate_content / embed_content call
    is automatically recorded as a generation span. Applied once at startup.
    """
    try:
        from google.genai.models import AsyncModels
    except Exception as exc:
        logger.warning(f"[tracing] google-genai auto-instrument unavailable: {exc}")
        return

    if getattr(AsyncModels, "_lf_patched", False):
        return  # already patched (e.g. hot-reload)

    _orig_gen = AsyncModels.generate_content
    _orig_emb = AsyncModels.embed_content

    async def _traced_gen(self, **kwargs):
        if not _enabled or _client is None or _manual_gen.get():
            return await _orig_gen(self, **kwargs)
        model = kwargs.get("model", "unknown")
        op    = _caller_operation()
        inp   = _trunc(_contents_to_text(kwargs.get("contents")))
        started = False
        try:
            with _ident_cm(), _client.start_as_current_observation(
                name=f"llm:{op}",
                as_type="generation",
                model=model,
                input=inp,
                metadata={"operation": op},
            ):
                started = True
                resp = await _orig_gen(self, **kwargs)
                try:
                    record_gemini_usage(resp)
                except Exception:
                    pass
                return resp
        except Exception:
            if started:
                raise
            return await _orig_gen(self, **kwargs)

    async def _traced_emb(self, **kwargs):
        if not _enabled or _client is None or _manual_gen.get():
            return await _orig_emb(self, **kwargs)
        model = kwargs.get("model", "unknown")
        op    = _caller_operation()
        inp   = _trunc(_contents_to_text(kwargs.get("contents")))
        started = False
        try:
            with _ident_cm(), _client.start_as_current_observation(
                name=f"embed:{op}",
                as_type="generation",
                model=model,
                input=inp,
                metadata={"operation": op},
            ):
                started = True
                resp = await _orig_emb(self, **kwargs)
                try:
                    record_gemini_usage(resp, output=None)
                except Exception:
                    pass
                return resp
        except Exception:
            if started:
                raise
            return await _orig_emb(self, **kwargs)

    AsyncModels.generate_content = _traced_gen
    AsyncModels.embed_content    = _traced_emb
    AsyncModels._lf_patched      = True
    logger.info("[tracing] google-genai AsyncModels auto-instrumented (generate_content + embed_content)")
