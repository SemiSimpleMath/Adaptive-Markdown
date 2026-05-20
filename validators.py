"""Backend-side doc validators. Port of the JS validators in index.html so
the post_tool_use_hook can run them synchronously and feed any errors back
to the agent as `additionalContext`, closing the auto-correct loop.

JS syntax check shells out to `node --check` — script grammar (not
function-body grammar), so legacy HTML comments parse, top-level `return`
correctly errors, etc. Node is already required by the Claude Agent SDK,
so no new dependency. If node isn't on PATH, JS validation degrades to a
no-op (still catches CSS / SVG / directive issues).

CSS / SVG / directive checks are pure-stdlib Python.

Runtime-agnostic: the Claude runtime calls this from `PostToolUse` for
per-edit validate+revert+retry; the Codex runtime calls it from its
per-turn finalizer for validate+revert (no agent-loop retry — see
ROADMAP "Validator retry-loop parity" for the gap and trigger).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from typing import TypedDict


class ValidationError(TypedDict):
    kind: str       # "script" | "style" | "svg" | "directive"
    line: int       # 1-indexed line in the source
    message: str    # human-readable error


_NODE_PATH: str | None = None


def _node() -> str | None:
    """Resolve the `node` binary path once and cache. Returns None if absent."""
    global _NODE_PATH
    if _NODE_PATH is None:
        _NODE_PATH = shutil.which("node") or ""
    return _NODE_PATH or None


def _mask_markdown_code(src: str) -> str:
    """Replace fenced code blocks, inline code spans, and HTML comments
    with same-length runs of spaces (newlines preserved) so HTML-tag
    extraction regexes don't match literal `<script>` text that appears
    *inside* markdown code (e.g. prose mentioning `<script>` in backticks).

    Without this, a doc with prose like ``If you can express it in a
    `<script>` tag …`` causes the script-tag regex to match the opening
    `<script>` inside the inline code span, then non-greedy-capture the
    next ~100 lines (markdown prose + CSS + real JS) as the script
    "body" and feed all of that into `node --check`. Almost any colon,
    `=`, or directive marker downstream then "errors" — but the real
    bug is the false match at the inline code span.

    Line numbers must be preserved so the file-line we report in errors
    still points at the right place — hence we substitute spaces, not
    deletion, and keep newlines intact.
    """
    out = list(src)

    # 1. Fenced code blocks: ```...``` or ~~~...~~~. Match on opening
    # fence at start of line, run until a matching closing fence.
    fence_pattern = re.compile(
        r"^([ \t]*)(```+|~~~+)[^\n]*\n([\s\S]*?)^\1\2[^\n]*$",
        re.MULTILINE,
    )
    for m in fence_pattern.finditer(src):
        for i in range(m.start(), m.end()):
            if out[i] != "\n":
                out[i] = " "

    # 2. HTML comments: <!-- ... -->. Multi-line allowed.
    for m in re.finditer(r"<!--[\s\S]*?-->", src):
        for i in range(m.start(), m.end()):
            if out[i] != "\n":
                out[i] = " "

    # 3. Inline code spans: backtick-delimited, single line. Match the
    # CommonMark-ish form (1+ backticks, same count to close).
    # We do this on the now-fence-stripped buffer to avoid touching
    # backticks inside fences (already neutralised above).
    interim = "".join(out)
    for m in re.finditer(r"(`+)(?:(?!\1)[^\n])*\1", interim):
        for i in range(m.start(), m.end()):
            if out[i] != "\n":
                out[i] = " "

    return "".join(out)


import am_html  # noqa: E402


def _extract_blocks(src: str, tag: str) -> list[dict]:
    """Pull <tag>...</tag> bodies out of raw source. Records 1-indexed line
    of each opening tag for error reporting. Markdown code spans / fenced
    blocks / HTML comments are masked out first so literal `<script>`
    text inside them doesn't get treated as a real tag.

    Returns dicts with `body`, `line`, and `attrs` (a parsed dict of
    the opening tag's attributes — callers can inspect e.g. `type` to
    decide whether the body is code or opaque data). Delegates the
    actual HTML-block extraction to am_html.find_blocks, which handles
    edge cases (script opacity per browser, `>` inside quoted attrs,
    multi-line opening tags) that the previous regex pattern missed."""
    # Mask code spans / fences / HTML comments so a literal `<script>`
    # mentioned inside backticks or a code block doesn't get extracted
    # as a real tag. _mask_markdown_code preserves length, so offsets
    # from the masked source are valid in the original.
    masked = _mask_markdown_code(src)
    out = []
    for b in am_html.find_blocks(masked, tag):
        # Pull body from the ORIGINAL source so any backticks-mentioned
        # tag names inside the real script body aren't blanked.
        out.append({
            "body": src[b.body_start:b.body_end],
            "line": b.line,
            "attrs": b.attrs,
        })
    return out


# JS MIME types per HTML spec (whatwg, "JavaScript MIME type essence match").
# Anything outside this set (and not "module" / empty / missing) is a "data
# block" — the browser doesn't execute it, so we mustn't syntax-check it.
_JS_MIME_TYPES = frozenset({
    "text/javascript",
    "application/javascript",
    "text/ecmascript",
    "application/ecmascript",
    "application/x-javascript",
    "text/x-javascript",
    "text/jscript",
    "text/livescript",
})


def _is_js_script(attrs: dict) -> bool:
    """Whether a <script ...> tag's body should be parsed as JavaScript.

    Per HTML spec, a script is a classic JS script if `type` is missing,
    empty, or a JS MIME type; `type="module"` is ESM (also JS). Any
    other type (`application/json`, `application/ld+json`,
    `application/vnd.recordare.musicxml+xml`, `text/x-handlebars`, …)
    makes the tag a *data block*: the browser hands the body to whoever
    asks for it via DOM, never executes it. The validator must do the
    same and not run `node --check` on opaque data."""
    raw = (attrs.get("type") or "").strip().lower()
    if not raw or raw == "module":
        return True
    essence = raw.split(";", 1)[0].strip()
    return essence in _JS_MIME_TYPES


_NODE_STDIN_LINE_RE = re.compile(r"^\[stdin\]:(\d+)$", re.MULTILINE)
_NODE_ERROR_RE = re.compile(r"^([A-Z][a-zA-Z]*Error: .+)$", re.MULTILINE)


def _check_js(body: str, line: int) -> ValidationError | None:
    """Parse the script body with `node --check` (real script grammar, not
    `new Function`'s function-body grammar) and translate Node's position
    info into an absolute file line for the agent.

    Why not `new Function(body)`: function-body grammar accepts things real
    scripts don't (top-level `return`) and rejects things real scripts do
    (legacy HTML comments). It also gives error messages with no offsets,
    so the agent has nothing to grep for. `node --check` matches script
    semantics and emits `[stdin]:N\\nsource line\\n  ^\\nError: …`."""
    if not body.strip():
        return None
    node = _node()
    if node is None:
        return None  # No Node — silently skip JS check
    try:
        result = subprocess.run(
            [node, "--check"],
            input=body,
            text=True,
            # Force UTF-8 on the subprocess pipes. Default is the platform
            # locale (cp1252 on Windows), which raises UnicodeEncodeError
            # on any non-Latin-1 character in the script body (ε, π, ⟹, …).
            # `start.py` respawns Python with `PYTHONUTF8=1` so this happens
            # to work in production, but the standalone-import path
            # otherwise crashes hard. Pinning encoding removes the landmine.
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=5,
        )
        if result.returncode == 0:
            return None
        stderr = result.stderr or result.stdout or ""
        # 1. Body-relative line number → absolute file line.
        # `[stdin]:N` means line N of the script body; the file line of the
        # `<script>` opening tag is `line`, so the actual file line is
        # line + N - 1 (body line 1 is the same row as the opening tag,
        # everything after the first newline shifts by one).
        m = _NODE_STDIN_LINE_RE.search(stderr)
        body_line = int(m.group(1)) if m else 1
        abs_line = line + max(0, body_line - 1)
        # 2. Pull the SyntaxError/ReferenceError text out of the multi-line
        # node output. If we can't find it, fall back to the first line.
        err_m = _NODE_ERROR_RE.search(stderr)
        err_msg = (err_m.group(1) if err_m else stderr.strip().split("\n")[0])
        # 3. Pull the source-line + caret context (the two lines right after
        # the `[stdin]:N` header). That's the agent's locator.
        ctx_lines: list[str] = []
        lines_out = stderr.split("\n")
        for i, ln in enumerate(lines_out):
            if ln.startswith("[stdin]:"):
                if i + 1 < len(lines_out) and lines_out[i + 1].strip():
                    ctx_lines.append(lines_out[i + 1])
                if i + 2 < len(lines_out) and "^" in lines_out[i + 2]:
                    ctx_lines.append(lines_out[i + 2])
                break
        ctx_str = ""
        if ctx_lines:
            ctx_str = "\n    " + "\n    ".join(ctx_lines)
        return {
            "kind": "script", "line": abs_line,
            "message": f"{err_msg[:300]}{ctx_str}",
        }
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def _check_css(body: str, line: int) -> ValidationError | None:
    """Brace-balance heuristic. Matches the JS validator behavior."""
    stripped = re.sub(r"/\*[\s\S]*?\*/", "", body)
    stripped = re.sub(r"\"[^\"]*\"|'[^']*'", '""', stripped)
    opens = stripped.count("{")
    closes = stripped.count("}")
    if opens != closes:
        return {
            "kind": "style", "line": line,
            "message": f"unbalanced braces: {opens} '{{' vs {closes} '}}'",
        }
    return None


def _check_svg(body: str, line: int) -> ValidationError | None:
    if not body.strip():
        return None
    wrapped = body if body.strip().startswith("<svg") else (
        f'<svg xmlns="http://www.w3.org/2000/svg">{body}</svg>'
    )
    try:
        ET.fromstring(wrapped)
        return None
    except ET.ParseError as e:
        return {
            "kind": "svg", "line": line,
            "message": str(e)[:200],
        }


def validate_doc(text: str) -> list[ValidationError]:
    """Run all validators on a markdown source string. Returns a flat list
    of errors with kind / line / message — empty list means valid.

    Structured content (callouts, figures, theorems, locked blocks) is
    plain HTML in the source — markdown-it passes those blocks through
    verbatim and the renderer applies CSS by class. There is no
    directive grammar to police here; if the agent writes malformed
    HTML the browser is forgiving and the user sees a visual glitch
    rather than a hard failure."""
    errors: list[ValidationError] = []

    # Tag-balance check: each <script> opening should have exactly one
    # matching </script>. Catches the agent regression where Haiku writes
    # `<\/script>` (escaped) as the closing tag, thinking the backslash
    # makes it safe — actually it makes the browser NOT terminate the
    # script, the body extends past EOF, the script throws on the markdown
    # that follows, and the button (or whatever) never gets created.
    #
    # Same shape for <style>. Mask markdown code spans first so prose
    # mentions like `<script>` in backticks don't count.
    masked = _mask_markdown_code(text)
    for tag in ("script", "style"):
        opens = len(re.findall(rf"<{tag}\b", masked, re.IGNORECASE))
        closes = len(re.findall(rf"</{tag}\b", masked, re.IGNORECASE))
        if opens != closes:
            # Find the line of the first orphan open (best heuristic).
            line = 1
            m = re.search(rf"<{tag}\b", masked, re.IGNORECASE)
            if m:
                line = masked[:m.start()].count("\n") + 1
            esc_present = "<\\/" + tag + ">" in text
            esc_hint = ""
            if esc_present:
                esc_hint = (
                    f" Found `<\\/{tag}>` (escaped) in the source — that's "
                    f"wrong as a closing tag. The HTML parser does NOT "
                    f"recognize the backslash form; it treats the body as "
                    f"unterminated. Write `</{tag}>` plain — the escape "
                    f"trick is only correct INSIDE a JS string literal "
                    f"(e.g. `const s = \"<\\/{tag}>\"`), never as the "
                    f"actual closing tag."
                )
            errors.append({
                "kind": f"{tag}-tag-balance",
                "line": line,
                "message": (
                    f"<{tag}> open / close count mismatch: {opens} opening "
                    f"tag(s), {closes} closing tag(s)."
                    + esc_hint
                ),
            })

    for s in _extract_blocks(text, "script"):
        if not _is_js_script(s["attrs"]):
            continue  # data block — body is opaque, not JS
        e = _check_js(s["body"], s["line"])
        if e:
            errors.append(e)
    for s in _extract_blocks(text, "style"):
        e = _check_css(s["body"], s["line"])
        if e:
            errors.append(e)
    for s in _extract_blocks(text, "svg"):
        e = _check_svg(s["body"], s["line"])
        if e:
            errors.append(e)
    return errors


def format_for_agent(errors: list[ValidationError], file_path: str) -> str:
    """Render errors as a chat-style message the agent will see in
    `additionalContext`. Be specific and prescriptive about the fix."""
    lines = [
        f"EDIT REJECTED. Your last edit to {file_path} contained a "
        "syntax error and was REVERTED — the file on disk is back to its "
        "pre-edit content. Do NOT tell the reader the edit succeeded.",
        "",
        "Errors:",
    ]
    for e in errors:
        lines.append(f"  [{e['kind']} @ line {e['line']}]  {e['message']}")
    lines.append("")
    lines.append(
        "Next action: either fix the syntax and Edit again, or — if you "
        "cannot figure out the fix in one more try — stop and tell the "
        "reader plainly that the edit failed and what you tried. Do not "
        "claim success without a passing Edit. The validator runs on "
        "every Edit/Write."
    )
    return "\n".join(lines)
