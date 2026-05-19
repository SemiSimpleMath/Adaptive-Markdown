"""LLM-based source-to-markdown conversion (PDF, LaTeX).

Called by am_upload when a reader drops a PDF or .tex onto +Doc. Hits
the Anthropic API directly (urllib + stdlib JSON — no agent loop, no
chat round-trip) and returns the converted markdown, or None if the
API is unavailable or the source is over the per-call cap so the
caller can fall back to markitdown / the agent-mediated path.

Sized at one full paper per call; large documents will get a model
auto-pick + chunk + continuation pass later (see ROADMAP).
"""
from __future__ import annotations

import asyncio
import os

# Both PDF and LaTeX paths use the same model / max-tokens budget. PDF
# is the larger of the two because it ships pixels; LaTeX is much cheaper.
_PDF_CLAUDE_MAX_BYTES = 30 * 1024 * 1024  # margin under Anthropic's 32MB limit
_PDF_CLAUDE_MODEL = "claude-sonnet-4-5"
_PDF_CLAUDE_MAX_TOKENS = 16384

_PDF_CONVERT_PROMPT = (
    "Convert this PDF to adaptive-markdown. Strict rules:\n"
    "- Use `#` for the document title, `##` for top-level sections, "
    "`###` for subsections.\n"
    "- Use markdown bullet lists (`- `) for any list of items in the source.\n"
    "- Preserve inline emphasis: bold becomes `**...**`, italic becomes "
    "`_..._`.\n"
    "- Preserve math: inline as `$...$`, display as `$$...$$`. If the source "
    "has expressions written as plain text (e.g. `x(t+1) = f(x(t))`), render "
    "them as math.\n"
    "- Tables become GitHub-flavored markdown tables.\n"
    "- Do NOT add commentary, summary, or any preamble. Output ONLY the "
    "markdown body of the document.\n"
    "- Preserve all paragraphs verbatim where possible; do not paraphrase."
)

# LaTeX-source conversion uses the same Claude path as PDF but takes the
# .tex content as text rather than as a rasterized document. Same model,
# same budget, same fallback behavior (returns None on no-key / oversize /
# API failure so the caller can fall through to the agent-mediated path).
_TEX_CLAUDE_MAX_BYTES = 500 * 1024  # 500KB of source — plenty for one paper
_LLM_CONVERT_TEXT_EXTS = {".tex"}

_TEX_CONVERT_PROMPT = (
    "Convert this LaTeX source to adaptive-markdown. Strict rules:\n"
    "- Drop the preamble (`\\documentclass`, `\\usepackage`, `\\newcommand`, "
    "etc.). Custom macros that are actually used in the body should be "
    "expanded inline.\n"
    "- `\\title{...}` → `#`. `\\section` → `##`. `\\subsection` → `###`. "
    "`\\subsubsection` → `####`.\n"
    "- Preserve math: inline as `$...$`, display as `$$...$$`. KaTeX-"
    "compatible syntax. Convert `\\[...\\]` → `$$...$$` and `\\(...\\)` → "
    "`$...$`.\n"
    "- `\\begin{itemize}` → markdown `- ` bullet list. `\\begin{enumerate}` "
    "→ markdown `1.` numbered list.\n"
    "- amsthm environments (`\\begin{theorem}`, `\\begin{lemma}`, "
    "`\\begin{proposition}`, `\\begin{corollary}`, `\\begin{definition}`, "
    "`\\begin{example}`, `\\begin{proof}`) → "
    "`<section class=\"<kind>\">...</section>` with an inner `<h3>` if the "
    "source had a name in `[...]`.\n"
    "- `\\begin{figure}` → `<figure>...</figure>`. `\\caption{...}` → "
    "`<figcaption>...</figcaption>`. TikZ source stays as ```tikz fenced "
    "blocks.\n"
    "- `\\begin{tabular}` → GitHub-flavored markdown table.\n"
    "- `\\textbf{...}` → `**...**`. `\\emph{...}` / `\\textit{...}` → "
    "`_..._`. `\\texttt{...}` → backtick-quoted code.\n"
    "- `\\cite{key}` → `[key]`. `\\ref{x}` → drop the ref-target ID but "
    "keep the surrounding sentence intact.\n"
    "- Strip `%`-comment lines.\n"
    "- Do NOT add commentary or preamble. Output ONLY the markdown body.\n"
    "- Preserve all content. Do not paraphrase. Do not summarize."
)


async def _convert_tex_via_claude(source_text: str) -> str | None:
    """Send LaTeX source to Claude and ask for adaptive-markdown back.
    Returns the converted text on success, or None if Claude is
    unavailable / failed (caller falls back to agent-mediated conversion)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[upload:tex:claude] no ANTHROPIC_API_KEY — skipping Claude path",
              flush=True)
        return None
    enc = source_text.encode("utf-8")
    if len(enc) > _TEX_CLAUDE_MAX_BYTES:
        print(f"[upload:tex:claude] source too large ({len(enc)}B > "
              f"{_TEX_CLAUDE_MAX_BYTES}B) — skipping Claude path", flush=True)
        return None

    def _call() -> str | None:
        import json as _json
        import urllib.request
        import urllib.error
        body = {
            "model": _PDF_CLAUDE_MODEL,
            "max_tokens": _PDF_CLAUDE_MAX_TOKENS,
            "messages": [{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "--- LaTeX source ---\n"
                            + source_text
                            + "\n--- end source ---\n\n"
                            + _TEX_CONVERT_PROMPT
                        ),
                    },
                ],
            }],
        }
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=_json.dumps(body).encode("utf-8"),
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                payload = _json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")[:500]
            print(f"[upload:tex:claude] HTTP {e.code}: {err}", flush=True)
            return None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"[upload:tex:claude] network error: {e!r}", flush=True)
            return None
        parts = payload.get("content", []) or []
        text = "\n".join(
            p.get("text", "") for p in parts if p.get("type") == "text"
        ).strip()
        return text or None

    return await asyncio.to_thread(_call)


async def _convert_pdf_via_claude(pdf_bytes: bytes) -> str | None:
    """Send a PDF to Claude and ask for adaptive-markdown back. Returns the
    converted text on success, or None if Claude is unavailable / failed
    (caller should fall back to markitdown)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[upload:pdf:claude] no ANTHROPIC_API_KEY — skipping Claude path",
              flush=True)
        return None
    if len(pdf_bytes) > _PDF_CLAUDE_MAX_BYTES:
        print(f"[upload:pdf:claude] PDF too large ({len(pdf_bytes)}B > "
              f"{_PDF_CLAUDE_MAX_BYTES}B) — skipping Claude path", flush=True)
        return None

    def _call() -> str | None:
        import base64
        import json as _json
        import urllib.request
        import urllib.error
        body = {
            "model": _PDF_CLAUDE_MODEL,
            "max_tokens": _PDF_CLAUDE_MAX_TOKENS,
            "messages": [{
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": base64.b64encode(pdf_bytes).decode("ascii"),
                        },
                    },
                    {"type": "text", "text": _PDF_CONVERT_PROMPT},
                ],
            }],
        }
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=_json.dumps(body).encode("utf-8"),
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                payload = _json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")[:500]
            print(f"[upload:pdf:claude] HTTP {e.code}: {err}", flush=True)
            return None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"[upload:pdf:claude] network error: {e!r}", flush=True)
            return None
        parts = payload.get("content", []) or []
        text = "\n".join(
            p.get("text", "") for p in parts if p.get("type") == "text"
        ).strip()
        return text or None

    return await asyncio.to_thread(_call)
