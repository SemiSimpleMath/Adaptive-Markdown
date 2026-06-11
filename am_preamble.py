"""Pure helpers for building the per-turn chat preamble.

Kept separate from `am_ws` (which pulls in aiohttp and the whole runtime
state) so the inline/skip decision can be unit-tested in isolation: it is
pure — same inputs, same outputs, no I/O, no globals.
"""
from __future__ import annotations

import hashlib


def build_doc_context(
    doc: str,
    rel_for_agent: str,
    doc_text: str | None,
    inline_cap: int,
    prev_sig: str | None,
) -> tuple[list[str], str | None, bool]:
    """Decide how to present the active doc to the agent this turn.

    Pure decision over four cases:

    - **Unreadable** (`doc_text is None`): the backend couldn't load the
      file. Emit a minimal "this is the file" pointer.
    - **Over the inline cap** (`len(doc_text) > inline_cap`): too large to
      inline — Read-on-demand. The Opus path sets `inline_cap < 0` so this
      branch always fires for it (the Read tool result lands in cached
      history, billing the doc once per session rather than once per turn).
    - **Unchanged since last inline** (`sha256(doc_text) == prev_sig`): the
      full copy is already in cached conversation history. Emit a short
      pointer instead of re-sending the body — this is the cost win.
    - **Changed or first sight**: inline the full body, and return its
      sha256 for the caller to persist as the new signature.

    Returns ``(preamble_lines, sig_to_store, inlined)``:

    - ``preamble_lines`` — text to append to the preamble (one entry).
    - ``sig_to_store`` — the new signature to persist *iff* we inlined the
      full body this turn; ``None`` otherwise (a skip needs no change, and
      the non-inline paths never record a signature).
    - ``inlined`` — ``True`` when the agent has the doc's full content in
      context (inlined this turn or already cached from a prior turn).
      Gates the "no need to Read" note on focused blocks and the per-doc
      agent-skill note in the caller.
    """
    # Unreadable: minimal pointer, nothing in context.
    if doc_text is None:
        return (
            [
                f'The reader is viewing "{doc}" — file path '
                f"`{rel_for_agent}`. When you need to edit, "
                "this is the file."
            ],
            None,
            False,
        )

    # Over the inline cap (incl. the Opus `inline_cap < 0` case): the agent
    # Reads on demand; the result is cached in history for the session.
    if len(doc_text) > inline_cap:
        return (
            [
                f'The reader is viewing "{doc}" — file path '
                f"`{rel_for_agent}` ({len(doc_text)} bytes — "
                "too large to inline). When you need to edit, "
                "this is the file. Use `Read` with `offset`/"
                "`limit` to load specific ranges rather than "
                "the whole file."
            ],
            None,
            False,
        )

    lines = doc_text.count("\n") + 1
    sig = hashlib.sha256(doc_text.encode("utf-8")).hexdigest()

    # Unchanged since we last inlined it this conversation — the full copy
    # is already in cached history above. Point at it; don't re-send.
    if prev_sig == sig:
        return (
            [
                f'The reader is viewing "{doc}" — file path '
                f"`{rel_for_agent}` ({lines} lines, "
                f"{len(doc_text)} bytes). Its contents are "
                "UNCHANGED since the full copy already inlined "
                "earlier in this conversation (above in your "
                "context); that copy is current and "
                "authoritative. Do NOT Read this file. When "
                "you need to edit, this is the file."
            ],
            None,
            True,
        )

    # Changed or first sight: inline the full body, report the new sig.
    return (
        [
            f'The reader is viewing "{doc}" — file path '
            f'`{rel_for_agent}` ({lines} lines, '
            f"{len(doc_text)} bytes). When you need to edit, "
            "this is the file. The current contents are "
            "inlined below — do NOT call Read on this file "
            "unless YOU have edited it since this preamble "
            "(your own edits invalidate the inlined copy):\n\n"
            f"=== doc:{rel_for_agent} ===\n{doc_text}\n=== end doc ==="
        ],
        sig,
        True,
    )


def build_skills_context(
    doc: str,
    skills: list[dict],
    prev_sig: str | None,
) -> tuple[list[str], str | None]:
    """Present the doc's sidecar skills (docs/<slug>/skills/*.md, ADR-002)
    to the agent. Mirrors build_doc_context's signature-skip: inline in
    full on first sight or change, short pointer when unchanged, and an
    explicit disregard note when previously-inlined skills disappear.

    ``skills`` is a list of {rel, name, raw} (raw = full file text incl.
    frontmatter). Returns ``(preamble_lines, sig_to_store)`` where
    ``sig_to_store`` is persisted by the caller iff not None (the empty
    string is the stored marker for "deletion already announced")."""
    if not skills:
        if prev_sig:  # previously inlined, now gone — say so once
            return (
                [
                    "The sidecar skill files previously inlined for this "
                    "doc have been removed — disregard those instructions."
                ],
                "",
            )
        return ([], None)
    blob = "\x00".join(s["rel"] + "\x00" + s["raw"] for s in skills)
    sig = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    names = ", ".join(s["name"] for s in skills)
    if prev_sig == sig:
        return (
            [
                f"This doc's working contract ({names}) under "
                f"`docs/{doc}/skills/` is already inlined earlier in this "
                "conversation and unchanged — it remains authoritative."
            ],
            None,
        )
    parts = [
        f"This doc has {len(skills)} sidecar skill file(s) under "
        f"`docs/{doc}/skills/` — the doc's working contract (voice, "
        "formatting, structural conventions specific to this doc). They "
        "override generic guidance in the global adaptive-markdown skill "
        "when they conflict; treat them as authoritative. They are NOT "
        "part of the doc body — never copy them into current.md; edit "
        "them at their own paths only when the reader explicitly asks. "
        "Contents inlined below:"
    ]
    for s in skills:
        parts.append(
            f"=== skill:{s['rel']} ===\n{s['raw']}\n=== end skill ==="
        )
    return (["\n\n".join(parts)], sig)
