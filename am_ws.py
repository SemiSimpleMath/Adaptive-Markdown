"""WebSocket protocol + run_turn driver.

Two functions:

  - `run_turn(text)` — feeds the prompt into the active runtime, streams
    each event back over the broadcast channel, prints a one-line
    `[tool]` audit entry per tool_use, and on `/cancel` propagates the
    asyncio.CancelledError so the WS handler's task transitions cleanly.

  - `ws_handler(request)` — registers the WS, ships the `ready` envelope
    (initial doc, doc list, model info), then loops over inbound
    messages. Dispatches `{type:"chat"}` (build the prompt, lazy-mint
    track_ids, kick `run_turn`), `{type:"cancel"}` (interrupt the
    in-flight turn), and `{type:"new_chat"}` (reset the runtime session).

`_safe_inline_doc_slug` is a read-side path validator used only by
ws_handler when it inlines the active doc into the chat preamble; kept
in this module since it's the only caller.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web, WSMsgType

from am_docs import (
    ROOT,
    _doc_path_for,
    list_all_docs,
)
from am_hooks import _summarize_tool, reset_runtime_session
from am_origin import _require_localhost_origin
from am_state import state
from am_tracking import mint_track_id_for

DEFAULT_DOC = "intro"  # slug; resolves to docs/intro/current.md


def _safe_inline_doc_slug(slug: str) -> Path | None:
    """Resolve a chat-context doc slug to the absolute path of its
    current.md iff it's safe to inline into the agent's preamble.

    Read-side counterpart to `_validate_agent_write_path`. The chat
    context comes from the client, so a malicious or buggy front-end
    could otherwise coax the backend into reading arbitrary files."""
    p = _doc_path_for(slug)
    if p is None or not p.is_file():
        return None
    return p


async def run_turn(text: str):
    async with state.busy:
        if not state.runtime:
            await state.broadcast({"type": "error", "text": "Agent runtime not initialized"})
            return
        # Per-turn audit metrics: prove (in stdout) that the agent is actually
        # making distinct tool calls vs. one-shotting a Write of the whole
        # doc. One [tool] line per tool_use event, one [turn] summary line.
        t0 = time.time()
        tool_count = 0
        cost: float | None = None
        cancelled = False
        try:
            async for event in state.runtime.run_turn(text):
                etype = event.get("type", "")
                if etype == "tool_use":
                    tool_count += 1
                    # A malformed payload shouldn't abort the turn — the audit
                    # log is a diagnostic, not load-bearing.
                    try:
                        summary = _summarize_tool(
                            event.get("name", ""), event.get("input"),
                        )
                    except Exception as e:
                        summary = (
                            f"{event.get('name', '?')} "
                            f"(summary failed: {type(e).__name__})"
                        )
                    print(f"[tool] {summary}", flush=True)
                elif etype == "turn_done":
                    c = event.get("cost_usd")
                    if isinstance(c, (int, float)):
                        cost = c
                await state.broadcast(event)
        except asyncio.CancelledError:
            # Reader hit /cancel. Tell the UI, mark the turn done, and
            # propagate so the surrounding task transitions to cancelled.
            cancelled = True
            print("[turn] cancelled by reader", flush=True)
            await state.broadcast({
                "role": "assistant", "type": "text",
                "text": "_Turn cancelled by reader._",
            })
            await state.broadcast({"type": "turn_done", "cancelled": True})
            raise
        except Exception as e:
            import traceback
            print(f"[turn] FAILED with {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()
            await state.broadcast({
                "type": "error",
                "text": f"agent error: {type(e).__name__}: {e}",
            })
        finally:
            dt = time.time() - t0
            cost_str = f", cost=${cost:.4f}" if cost is not None else ""
            tag = " (cancelled)" if cancelled else ""
            print(f"[turn] done in {dt:.1f}s, {tool_count} tool call(s){cost_str}{tag}",
                  flush=True)


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    _require_localhost_origin(request)
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    async with state.client_lock:
        state.clients.add(ws)

    await ws.send_json({
        "type": "ready",
        "doc": DEFAULT_DOC,
        "docs": list_all_docs(),
        "provider": state.current_provider,
        "model": state.current_model,
        "models": state.runtime.model_aliases if state.runtime else [],
    })

    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except Exception:
                continue

            if data.get("type") == "chat":
                text = (data.get("text") or "").strip()
                ctx = data.get("context") or {}
                if not text:
                    continue

                # Optional per-turn model switch. Resets the runtime session
                # (model is set at session init, not per-query).
                requested_model = data.get("model") or ctx.get("model")
                if requested_model and state.runtime:
                    target = state.runtime.resolve_model(requested_model)
                    if target != state.current_model:
                        await state.broadcast({
                            "role": "assistant",
                            "type": "text",
                            "text": f"_Switching model → {target} (new conversation)_\n",
                        })
                        await reset_runtime_session(target)

                doc = (ctx.get("doc") or "").strip() or None
                selections = ctx.get("selections") or []
                if not isinstance(selections, list):
                    selections = []
                # Insertion is the "click in a gap between blocks" affordance.
                # Mutually exclusive with selections client-side.
                insertion = ctx.get("insertion")
                if not isinstance(insertion, dict):
                    insertion = None

                # Lazy-mint track_ids for any focused selections that lack one.
                # The block becomes stably-identifiable from this point on.
                doc_changed_after_mint = False
                if doc:
                    doc_path = ROOT / doc
                    for s in selections:
                        if isinstance(s, dict) and not s.get("track_id"):
                            new_id = mint_track_id_for(doc_path, s)
                            if new_id:
                                s["track_id"] = new_id
                                if not s.get("id"):
                                    s["id"] = new_id
                                doc_changed_after_mint = True
                                print(f"[mint] track_id={new_id} for "
                                      f"selection label={s.get('label')!r}",
                                      flush=True)
                if doc_changed_after_mint and doc:
                    await state.broadcast({"type": "doc_changed", "doc": doc})

                # Echo user's text verbatim.
                await state.broadcast({"role": "user", "type": "text", "text": text})

                # Augment prompt with doc contents (inlined) + focused blocks.
                # Pre-loading the active doc into context spares the agent a
                # Read tool round-trip every turn — for AM, the doc IS the
                # subject of the conversation, so the agent will Read it
                # almost always. Inlining is amortized by prompt caching
                # across turns where the doc doesn't change. Soft cap of
                # 50 KB; above that, fall back to Read-on-demand.
                INLINE_DOC_CAP = 50 * 1024
                preamble = []
                # Current wall-clock time. LLMs have no clock and otherwise
                # confabulate plausible-but-wrong digits when a doc-skill
                # asks them to timestamp a note. Inject every turn so the
                # value is fresh mid-conversation. Local time with tz name
                # is what authors actually want in their notes; UTC trailer
                # keeps it unambiguous if the agent ever logs or compares.
                _now_local = datetime.now().astimezone()
                _local_str = _now_local.strftime("%Y-%m-%d %H:%M:%S %Z").strip()
                _offset = _now_local.strftime("%z")  # e.g., "-0800"
                _offset_pretty = (
                    _offset[:3] + ":" + _offset[3:] if _offset else ""
                )
                _utc_str = _now_local.astimezone(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                preamble.append(
                    f"Current time: {_local_str}"
                    + (f" ({_offset_pretty})" if _offset_pretty else "")
                    + f" / {_utc_str}. "
                    "Use this when an instruction asks you to timestamp "
                    "something — do not guess or pattern-match the digits."
                )
                doc_inlined = False
                if doc:
                    safe_path = _safe_inline_doc_slug(doc)
                    doc_text: str | None = None
                    if safe_path is not None:
                        try:
                            doc_text = safe_path.read_text(encoding="utf-8")
                        except (OSError, UnicodeDecodeError):
                            doc_text = None
                    rel_for_agent = (
                        safe_path.relative_to(ROOT).as_posix()
                        if safe_path is not None else f"docs/{doc}/current.md"
                    )
                    if doc_text is not None and len(doc_text) <= INLINE_DOC_CAP:
                        lines = doc_text.count("\n") + 1
                        preamble.append(
                            f'The reader is viewing "{doc}" — file path '
                            f'`{rel_for_agent}` ({lines} lines, '
                            f"{len(doc_text)} bytes). When you need to edit, "
                            "this is the file. The current contents are "
                            "inlined below — do NOT call Read on this file "
                            "unless YOU have edited it since this preamble "
                            "(your own edits invalidate the inlined copy):\n\n"
                            f"=== doc:{rel_for_agent} ===\n{doc_text}\n=== end doc ==="
                        )
                        doc_inlined = True
                        # Per-doc agent skill: any section in the body with
                        # class="agent-skill" is meta authored for you, not
                        # for the reader (the viewer hides it via CSS).
                        # Surface it explicitly so you don't gloss over it
                        # as "just a block in the doc."
                        if 'class="agent-skill"' in doc_text \
                                or "class='agent-skill'" in doc_text:
                            preamble.append(
                                "This doc carries one or more "
                                "`<section class=\"agent-skill\">…</section>` "
                                "blocks in its body. Those sections are the "
                                "doc's working contract — voice, formatting, "
                                "structural conventions specific to this doc. "
                                "They override generic guidance in the global "
                                "adaptive-markdown skill when they conflict. "
                                "Treat them as authoritative; preserve them "
                                "across edits unless the reader explicitly "
                                "asks you to change them."
                            )
                    elif doc_text is not None:
                        preamble.append(
                            f'The reader is viewing "{doc}" — file path '
                            f"`{rel_for_agent}` ({len(doc_text)} bytes — "
                            "too large to inline). When you need to edit, "
                            "this is the file. Use `Read` with `offset`/"
                            "`limit` to load specific ranges rather than "
                            "the whole file."
                        )
                    else:
                        preamble.append(
                            f'The reader is viewing "{doc}" — file path '
                            f"`{rel_for_agent}`. When you need to edit, "
                            "this is the file."
                        )
                if len(selections) == 1:
                    s = selections[0]
                    info = []
                    if s.get("id"):    info.append(f'id="{s["id"]}"')
                    if s.get("label"): info.append(f'label="{s["label"]}"')
                    info_str = ", ".join(info)
                    excerpt = s.get("excerpt", "")
                    excerpt_block = (
                        f"\n\nFocused block content (excerpt up to 2000 chars):\n"
                        f"```\n{excerpt}\n```"
                        if excerpt else ""
                    )
                    surrounding_note = (
                        "Surrounding context is in the inlined doc above — "
                        "no need to Read."
                        if doc_inlined else
                        "If you need surrounding context, Read the source file."
                    )
                    preamble.append(
                        f"The reader has clicked on a specific block, giving it "
                        f"focus ({info_str}).{excerpt_block}\n\n"
                        "When they say 'this', 'here', 'it', 'this section', "
                        "'under it', 'above it' — they mean this focused "
                        f"block. {surrounding_note}"
                    )
                elif insertion is not None:
                    before = insertion.get("before") if isinstance(insertion.get("before"), dict) else None
                    after = insertion.get("after") if isinstance(insertion.get("after"), dict) else None

                    def _fmt(b: dict | None, role: str) -> str:
                        if not b:
                            return f"  - {role}: (none — {'top' if role == 'block-before' else 'end'} of doc)"
                        bits = []
                        if b.get("id"):    bits.append(f'id="{b["id"]}"')
                        if b.get("label"): bits.append(f'label="{b["label"]}"')
                        ex = (b.get("excerpt") or "").strip()
                        if len(ex) > 240:
                            ex = ex[:240] + "…"
                        info = ", ".join(bits) if bits else "(unlabeled block)"
                        ex_line = f"\n    excerpt: ```{ex}```" if ex else ""
                        return f"  - {role}: {info}{ex_line}"

                    preamble.append(
                        "The reader has chosen an INSERTION POINT (not a "
                        "block selection). New content should be inserted "
                        "into the doc at this gap:\n\n"
                        + _fmt(before, "block-before") + "\n"
                        + _fmt(after, "block-after") + "\n\n"
                        "When they say 'insert here', 'add here', 'put it "
                        "here', they mean this gap. In your Edit, use the "
                        "block immediately before OR after as the "
                        "`old_string` anchor — match that block verbatim, "
                        "then put the new content directly before or after "
                        "it in `new_string`. Do not modify the surrounding "
                        "blocks; only insert between them. If the request "
                        "is also for content that conceptually belongs "
                        "elsewhere (e.g. 'add it at the top of the doc'), "
                        "honor the explicit instruction over this gap."
                    )
                elif len(selections) > 1:
                    blocks_str = "\n\n".join(
                        f"Block {i+1}: id={s.get('id') or '(none)'!r}, "
                        f"label={s.get('label') or ''!r}\n"
                        f"```\n{s.get('excerpt', '')}\n```"
                        for i, s in enumerate(selections)
                    )
                    preamble.append(
                        f"The reader has selected {len(selections)} blocks. When "
                        "they say 'these', 'them', 'this group', 'all of these' — "
                        "they mean these blocks. If the operation applies "
                        "independently to each block, consider parallel subagents.\n\n"
                        f"{blocks_str}"
                    )

                prompt = ("[" + "\n\n".join(preamble) + "]\n\n" + text
                          if preamble else text)
                print(f"[ws] chat doc={doc!r} selections={len(selections)}"
                      f"{' insertion' if insertion else ''}",
                      flush=True)
                # Pin the sandbox cwd to this turn's doc folder. The
                # PreToolUse Bash hook reads this when wrapping the
                # agent's command through `python -m sandbox`. Set BEFORE
                # the task is scheduled so the hook never sees a stale
                # (or None) slug from a previous turn.
                state.current_doc_slug = doc if isinstance(doc, str) else None
                turn_task = asyncio.create_task(run_turn(prompt))
                state.current_turn_task = turn_task

                def _clear_turn_task(t, _tt=turn_task):
                    if state.current_turn_task is _tt:
                        state.current_turn_task = None
                    state.current_doc_slug = None
                turn_task.add_done_callback(_clear_turn_task)

            elif data.get("type") == "cancel":
                # /cancel from the reader — interrupt the in-flight turn.
                # state.busy is held by run_turn for the whole turn, so this
                # is the only way to free the agent without restarting.
                t = state.current_turn_task
                if t and not t.done():
                    print("[ws] cancel requested", flush=True)
                    t.cancel()
                else:
                    await state.broadcast({
                        "role": "assistant", "type": "text",
                        "text": "_Nothing to cancel — no turn is running._",
                    })

            elif data.get("type") == "new_chat":
                model = data.get("model")
                asyncio.create_task(reset_runtime_session(model))

    finally:
        async with state.client_lock:
            state.clients.discard(ws)
    return ws
