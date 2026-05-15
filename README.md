# Adaptive Markdown

**Documents that are programmable objects.** Write plain markdown; a coding agent on the side rewrites, translates, illustrates, animates, or extends it in place — including arbitrary HTML, CSS, and JavaScript that ships *inside* the doc. The page you see is the file, executing.

Most agent-driven editing tools box the agent into a fixed component palette: it can rewrite text or fill in a sandboxed widget. **Here it has the full web platform** — every CSS rule, every `<script>` tag, every API the browser exposes — and the output is a normal `.md` file you can share, fork, and check into git. Dark mode toggle, animated headings, falling-letter effect, a snake game in the corner? All by asking, all stored as plain markdown.

[![Adaptive Markdown — demo](https://img.youtube.com/vi/H4MnFs8irm8/maxresdefault.jpg)](https://youtu.be/H4MnFs8irm8)

> ▶ [Watch the demo on YouTube](https://youtu.be/H4MnFs8irm8)

```
$ python start.py --claude
[start] provider=claude (flag, saved to .am-provider)
Adaptive Markdown listening on http://127.0.0.1:8090
```

---

## Prerequisites

- **Python 3.10+**
- An **Anthropic API key** for the default (Claude) runtime — get one at [console.anthropic.com](https://console.anthropic.com). The Claude Agent SDK draws from your API credits, not your Claude.ai subscription.
- *(Optional)* An **OpenAI API key** or installed **Codex CLI** for the experimental Codex runtime — see [Picking the runtime](#picking-the-runtime) below.
- A modern browser (Chrome / Edge / Firefox / Safari).

## Security & responsible use

This tool runs a capable coding agent on your local machine with access to your filesystem, your shell (via the `Bash` tool), and any document you load. **Treat this like running someone else's code locally** — because that's effectively what every chat turn is.

- **Use it with documents you authored or fully trust.** Document content becomes part of the agent's context. A malicious markdown or LaTeX file can include hidden instructions — natural-language prompt injection — aimed at the agent. Dropping in a `.md` you found on a sketchy corner of the internet is the same risk class as `curl … | bash`.
- **The agent edits files in place and can execute shell commands.** It's unlikely (and not its intent) to do something destructive like wipe a drive — but capable agents occasionally do unexpected things, and the only one accountable for the result is the person who pressed Run. Inspect the snapshot history (`↶ History` button), keep your own backups for anything irreplaceable, and run this on a workspace you can afford to lose.
- **This software is provided AS-IS** per the [MIT license](LICENSE). The authors assume no responsibility for misuse, data loss, or unexpected agent behavior. Use at your own risk.

## Install

```bash
git clone https://github.com/SemiSimpleMath/Adaptive-Markdown
cd Adaptive-Markdown

# (recommended) use a virtual environment
python -m venv .venv
# Windows:   .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate

pip install -r requirements.txt
```

Set your API key in your shell:

```bash
# macOS / Linux
export ANTHROPIC_API_KEY=sk-ant-...

# Windows PowerShell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```

Or copy `.env.example` to `.env` and set your keys there. Shell environment
variables take precedence over `.env`.

Run the backend:

```bash
python start.py --claude
```

Open <http://127.0.0.1:8090> in your browser. The tutorial doc loads by default.

After the first `--claude` (or `--codex`) run, the choice is remembered in a
local `.am-provider` file. Subsequent runs can just be `python start.py`.

## Picking the runtime

The launcher takes one of two flags on first run; bare `python start.py` uses
whatever was last picked:

```bash
python start.py --claude     # Claude Agent SDK — supported
python start.py --codex      # Codex CLI — experimental
python start.py              # use whatever was last picked
python start.py --port 9000  # any of the above + custom port
```

**Claude mode** runs the Claude Agent SDK in-process. Per-edit snapshots and
patches via the SDK's hook system; per-turn budget cap (`MAX_BUDGET_USD`). This
is the supported path for v0.1.

**Codex mode** wraps OpenAI's `codex exec` CLI. The substrate (history
snapshots, derived patches, alias bookkeeping, `examples/_pristine/`
protection) still works — but with these known limitations vs Claude mode:

- **Coarser snapshot granularity.** Codex CLI has no per-edit hook, so the
  runtime snapshots once per turn rather than per `Edit`. Block-level undo is
  still functional; you just get one history entry per chat turn instead of
  one per file edit.
- **No conversation memory by default.** Each `codex exec` is a fresh
  subprocess. The adapter replays prior turns into the prompt so the model has
  context, but it costs tokens. Use the **New chat** button (or a model
  switch) to reset history.
- **No per-turn budget cap.** Claude mode caps at `MAX_BUDGET_USD` (default
  $1/turn). Codex has no equivalent today — set spending alerts at your
  provider.
- **JSONL parsing is heuristic.** If the Codex CLI changes its event-stream
  format, the adapter may mislabel events or surface fewer tool indicators.
- **Requires Codex CLI installed.** Set `CODEX_COMMAND` if `codex` isn't on
  PATH. API-key mode reads `OPENAI_API_KEY`; ChatGPT-account mode uses Codex's
  own auth (`codex auth login`).

The runtime is captured at backend start, so switching providers requires a
restart (`python start.py --claude` / `--codex`). The model dropdown inside the
viewer only switches *within* the current provider.

## First run — the tutorial

`examples/intro.md` is itself the tutorial. It has four interactive sections:

1. **Rewrite for a kid** — click an ε-δ continuity definition, ask the agent to rewrite it for a 10-year-old.
2. **Translate from French** — click a paragraph about Évariste Galois, ask to translate.
3. **Add a figure** — click the unit-circle section, ask the agent to animate a point tracing it.
4. **Change the page itself** — ask for a dark-mode toggle, animated headings, or falling letters. The agent writes a literal `<script>` block into the markdown source.

Open the **Source** tab afterwards to see exactly what got added — no hidden framework, no component palette. Just markdown with embedded `<style>` and `<script>`.

## Configuration

For the common case, `python start.py --claude` / `--codex` is the simplest
way to pick a runtime. The env vars below are for finer control — model
selection, budget caps, Codex auth mode, port override.

Environment variables (all optional):

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | (required) | Your Anthropic API key. |
| `AGENT_PROVIDER` | `claude` | Agent runtime to use. Supported: `claude`, `codex` (experimental CLI adapter). |
| `MODEL` | `claude-haiku-4-5-20251001` | Claude default model. Override with a full SDK model id, or use the chat dropdown to switch between Haiku / Sonnet / Opus per session. |
| `CODEX_AUTH_MODE` | `api-key` if `OPENAI_API_KEY` is set, else `chatgpt` | Codex auth path. Use `api-key` for OpenAI API billing/models, `chatgpt` for ChatGPT-account auth. |
| `CODEX_FAST_MODEL` | `codex-mini-latest` in API-key mode, else `default` | Fast/safe Codex choice shown first. |
| `CODEX_MODEL` | same as `CODEX_FAST_MODEL` | Codex CLI model when `AGENT_PROVIDER=codex`. |
| `CODEX_COMMAND` | `codex` | Codex executable name or path for the experimental CLI adapter. |
| `CODEX_SANDBOX` | `workspace-write` | Sandbox mode passed to `codex exec`. |
| `CODEX_APPROVAL_POLICY` | `never` | Approval policy passed to Codex for non-interactive execution. |
| `MAX_BUDGET_USD` | `1.0` | Per-turn budget cap (USD). The SDK aborts a turn if costs would exceed it. |
| `PORT` | `8090` | Backend port. |

## The format

The full technical spec lives in [`FORMAT.md`](FORMAT.md) — file shape, reserved heading words, the two-tier ID model (anchor vs tracking), directive vocabulary, math conventions, embedded `<style>` / `<script>` semantics, sidecar files. Read that if you're writing tooling, an alternative agent, or a viewer port.

The agent's contract is at [`.claude/skills/adaptive-markdown/SKILL.md`](.claude/skills/adaptive-markdown/SKILL.md) — the prescriptive instructions the model loads at the start of every session.

## How it works

- **The doc** (`examples/*.md`) is a regular markdown file with optional `{#anchor}` attributes, `:::` directives, and inline `<style>` / `<script>` blocks.
- **The viewer** (`index.html`) renders the doc inside a sandboxed iframe and pipes click-to-focus selections, drops, and live updates over a WebSocket.
- **The agent** is either the Claude Agent SDK (default, supported) or the Codex CLI (experimental) via a thin provider abstraction in `agent_runtime/`. Both load the same skill text — at `.claude/skills/adaptive-markdown/SKILL.md` for Claude (auto-discovered by the SDK) and the mirror at `.agents/skills/adaptive-markdown/SKILL.md` for Codex (prepended per-turn into the prompt by the adapter). The skill is ~350 lines of plain text that teaches the model how to operate on the doc — preserve tracking IDs, write KaTeX-safe math, etc.
- **History & undo** — every pre-edit snapshot is captured under `.history/<doc-stem>/snap-…md`. The `↶ History` button in the doc header lets you scrub back. `↺ Reset` restores from `examples/_pristine/` — the ship-with originals are write-protected from the agent.

## Drop your own files

Drag any `.md` onto the viewer to open it. Drop a `.tex` (or `.txt` / `.rst` / `.org`) and the agent auto-converts it to adaptive markdown in place.

## What's not there yet

- Codex parity (today: experimental — coarser snapshot granularity, no budget cap, see [Picking the runtime](#picking-the-runtime))
- Multi-document workspace with cross-doc dependency graphs
- Pyodide for in-browser Python / SymPy in `:::computation` blocks
- Lean verification of formal theorem statements
- Hosted multi-user mode

Feature requests and ideas welcome via GitHub issues.

## License

- Code: [MIT](LICENSE)
- The skill text (`.claude/skills/adaptive-markdown/SKILL.md`) is the portable specification of the format — it's CC BY-SA 4.0 so derivatives stay open. See `SKILL-LICENSE`.

---

In a few years, no one will be reading journals on paper. Everyone will be interacting with articles, translating them instantly, exploring alternative proofs, asking questions, writing code on the spot into the document. This is what we're building toward.
