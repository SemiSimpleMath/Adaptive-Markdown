# Roadmap

What's planned, what's being worked on, what's still aspirational. Ordered roughly by expected ship order, not a commitment. Open a GitHub issue if you want to push on or contribute any of these — it helps avoid duplicate work.

## Active backlog

Things we've designed and intend to ship in the next iteration or two.

### Codex runtime parity

Codex mode landed experimental in v0.1. Bringing it to feature parity with Claude mode means closing several gaps:

- **Per-edit snapshot/patch granularity.** Today Codex snapshots once per changed file per turn; Claude does this per `Edit` tool call via SDK hooks. If/when Codex CLI exposes pre/post tool hooks, the adapter should match Claude's granularity.
- **Per-turn budget cap.** Claude caps spending at `MAX_BUDGET_USD` per turn. Codex has no equivalent — a wall-clock or token-count cap belongs in the adapter to prevent runaway turns.
- **Stable JSONL event parsing.** `codex_runtime._jsonl_to_event` heuristically maps event types and will likely break if Codex CLI changes its output schema. Either pin to a known-good CLI version or fall back gracefully on unrecognized event types.
- **Agent screenshot tool for visual iteration.** Visual edits (figures, animations, page-wide CSS, embedded UI) are currently the only thing the agent can't verify on its own — it writes code and waits for the human to say "the snake is invisible." Adding a `tools/screenshot_doc.py` the agent can invoke after a visual change, plus skill guidance on when to use it, would close the perception loop. Two phases: (1) reuse the existing monitor-capture script; agent reads via the existing image-aware `Read` tool. (2) `html2canvas` inside the iframe so the agent sees only the doc, not the whole screen — smaller context, no chat-panel feedback loop, no incidental privacy leak. Codex-side support depends on whether the model has vision in CLI exec mode and whether Codex's read tool surfaces image bytes; investigate.

### Authoring + reading

- **Component library / reusable snippets.** When the agent produces something good in one doc (a snake game, a working clock, a dark-mode toggle, a stopwatch, a specific math figure), the user can save it and reuse it later in any doc — *"save this as `snake-game`"* in chat, then *"insert the snake game"* anywhere else. Storage layer: a `components/` directory at the project root, one file per component, each file is itself a valid mini adaptive-markdown doc (frontmatter with name/intent/renderer/tags/provenance + body = one or more directives). Key nuance: because each saved component is plain markdown, the agent can *adapt on insertion* — *"insert the snake game but in red,"* *"insert the clock with Roman numerals"* — so the unit is a seed, not a frozen widget. Open issues to settle when building: ID uniquification on insert (two copies of `<canvas id="snake-game">` collide); self-containment metadata (a snake game is self-contained, a page-walking falling-letters effect isn't — components should declare scope); CSS class namespacing to prevent style leaks; later, a viewer-side Components panel for browse + drag-insert (chat is enough for v0). Sharing is free — a `components/` dir is just files, trivially zip-able, git-able.
- **Resource library / project assets.** A sidecar `resources/` directory at the project root for assets the user brings *in* — images, audio, video, data files (CSV / JSON / Parquet), reference docs, anything the agent should incorporate but didn't generate. The agent reads from it on request: *"use the unit-circle diagram at `resources/figures/unit-circle.png`"* → agent inserts the `<img>` / markdown image reference; *"plot the data in `resources/anscombe.csv`"* → agent reads the file, generates a `:::figure` with a script that visualizes it; *"reference Apéry's proof at `resources/papers/apery-2023.md`"* → agent inserts a cross-doc link with the right citation context. Companion to `components/`, opposite direction: components are agent-generated reusable code blocks the user *saves*; resources are user-provided assets the agent *uses*. Open issues: size limits and `.gitattributes`/`.gitignore` recommendations for large binaries; viewer-side Resources panel for browsing what's available (so the user doesn't need to remember filenames); discoverability for the agent (a manifest file, or just let it `Glob`); whether resources outside the project root are symlinked in or copied on drop; how missing resources behave when a doc is shared (graceful 404 in the iframe vs. a build-time check).
- **Variant blocks for audience presets.** Mark a block `{.locked}` and the agent emits sibling `:::variant {audience=novice|expert|...}` blocks. Viewer renders a tab strip; reader picks a default audience. Sidecar file keeps the main source clean.
- **Pending changes (review-before-accept).** Toggle a viewer mode where the agent writes proposed edits to a `<doc>.pending.json` sidecar; viewer overlays accept/reject UI on each proposed change. Source isn't touched until accept.
- **Annotation overlay.** A non-destructive layer for reader highlights, agent-marked dependencies ("blocks that depend on assumption X"), and query results. Sidecar JSON, toggleable in the viewer.

### Quality-of-life

- **Doc-list watchdog.** Today the viewer's doc dropdown only refreshes when files are added via the upload endpoint. A filesystem watcher would broadcast updates when `.md` files appear or disappear externally (git pull, drag in a file manager, etc.).
- **First-run setup card.** If no API key is visible at backend start, the viewer prompts for one rather than failing on the first chat turn.
- **"Refresh history-0 from working copy" action.** Today, manually-restored content in `examples/<name>.md` (via `git checkout`, paste from `keepers/`, an editor edit, etc.) isn't auto-captured as a new history-0 snap. The next Reset will jump *past* that manual restoration back to a stale earlier baseline, surprising the user. A small endpoint + viewer button — *"set current state as history-0"* — would wipe `.history/<stem>/` and re-seed from current content. Avoids the "I just restored content, why did Reset blow it away?" footgun. Until shipped, the workflow is `rm -rf .history/<stem>/ && restart` (the backend's `ensure_history_zero` then re-seeds on first sight).
- **Byte-exact snapshots (`save_snapshot` line-ending fix).** `save_snapshot` currently uses `Path.write_text(...)` with default `newline=None`, which on Windows translates `\n` → `\r\n` on write. Combined with `read_text(...)` doing the inverse on the way in, the captured snapshot may not byte-match the source working copy. Doesn't affect content equality (universal-newline comparisons all match), but causes spurious size deltas and `doc_changed` flicker after Reset/Restore. Fix: switch `save_snapshot` to `write_bytes` (or `write_text(..., newline="")`) and have callers pass through `read_bytes` where appropriate so the snapshot is a byte-for-byte copy of the source.

## Planned

Designed at a sketch level, not actively being implemented.

### Computation

- **Pyodide for `:::computation { lang=python }`.** In-browser Python (and SymPy) so figures with parameters can be re-run live in the doc. Adds ~6MB to first load — gated behind the directive so docs that don't need it stay light.

### Multi-document

- **Cross-doc references and dependency graph.** Anchor links work across documents; the system tracks backlinks (which docs cite this theorem). Substrate for the eventual case where the unit is the corpus, not the doc.
- **Browser-only deployment.** A single-file `am.html` bundling parser + viewer + agent loop calling the model API directly from JS. Loses local filesystem access; gains "share a URL, the recipient tries it in 30 seconds." Useful as the entry-point demo.

### Import

- **LaTeX importer hardening.** Turn the existing one-shot converter into a proper `import-tex` skill the agent invokes from chat. Wider AMS-TeX dialect support, better equation conversion, citation-graph extraction.
- **Chat-log → adaptive markdown.** Take a Slack/ChatGPT/design-review export and produce a structured doc with the substantive points extracted and the chronological log preserved as provenance.

## Directional

Likely correct, far from ready, no committed plan.

- **Lean verification.** Formal statements live alongside informal prose (`[Lean: theorem rolle : ...]`); Lean checks them; the agent keeps the formal statement in sync as the prose evolves. Proof bodies stay as `sorry`. The framing is "AI keeps the formal statement honest," not "AI proves your theorems."
- **Hosted multi-user mode.** Real-time collab, persistent storage, version-graph hosting, audit trail. Likely a separate product on top of the same format.
- **Multi-agent / multi-writer concurrency.** Real CRDT or 3-way merge for simultaneous editors. Today's optimistic-apply substrate handles solo authoring with a single agent; this is the future when a doc has multiple human + agent contributors.

## Open design questions

Decisions to make when the relevant feature lands. Captured here so contributors know they're not settled.

- **Snapshot semantics: pre-edit vs. post-edit (vs. HEAD-pointer).** Today the substrate is *pre-edit*: the `PreToolUse` hook captures the doc's state right before each agent `Edit`/`Write`. Snap N = "state before edit N." This makes patch derivation natural (snap N + current disk = before/after pair for the diff) but creates an asymmetry: the *result* of the most recent edit lives only in the working copy, not in any snap. Destructive UI actions (`/reset`, `/undo`, `/restore_snapshot`) used to silently destroy that latest state. **Mitigation in place (Option A):** each restore handler captures the current working copy as a safety snap before overwriting, so the History panel can always recover the just-lost state. Known v0 limitation: with safety snaps in `/undo`, double-clicking Undo can read the just-taken safety snap as the new "newest" and produce a "redo" instead of a deeper undo. Accepted as the rare edge case; the common case (single Reset/Undo destroying current state without recourse) is fixed. **Longer-term (Option B):** either move the snapshot to `PostToolUse` (snap N = "state after edit N"; latest snap always equals working copy; destruction-without-loss is structural rather than an explicit handler step), or introduce a HEAD-pointer model with proper undo/redo (snaps are immutable history; HEAD moves through them; "current state" is a derived concept). Option B is the correct answer for multi-writer / 3-way-merge work but isn't blocking today.
- **Sub-block patch granularity.** When the agent rewrites one sentence in a 10-sentence paragraph, do we replace the whole block (current behavior) or compute character-level deltas inside blocks (smaller wire size, more merge complexity)?
- **Patch ordering with multiple writers.** When patches arrive out of order, apply by `parent` lineage, by `ts`, or by some other rule? Not relevant in the single-writer phase; will be when phase 2 (3-way merge) lands.
- **Anchor ID uniqueness across docs.** With multi-doc workspaces, do `{#rolle}` anchors need to be globally unique, or namespaced by doc URL? Probably the latter.
- **Tracking ID lifecycle on archive / republish.** When a doc is frozen (published version), do new tracking IDs mint in the frozen copy, or does a fork happen? Relevant once publishing is a feature.

## Recently shipped

For context on pace.

- **Security pass** — iframe sandbox dropped `allow-same-origin` (null-origin doc context, can't read parent or backend); `serve_static` allowlisted to favicon + `examples/`/`docs/` .md only (no more `.env` exposure); `Origin` validation on WS + POST routes; drop-to-convert preview dialog (user sees the first 2KB before the agent does); upload blocklist of executable/binary extensions + NUL-byte guard; Claude's `Bash` and `WebFetch` removed from `allowed_tools` + denied in `.claude/settings.json`; path-validated `pre_tool_use_hook` rejects Claude writes outside `examples/*.md`/`docs/*.md`; SKILL.md gained a "Security boundaries" section + the `.agents/` mirror auto-syncs at startup; Codex runtime got a post-turn write-path validator (revert-from-snapshot for any modification to a protected file, with chat warning) and explicit `network_access=false` override. Real-browser + real-API test harnesses in `tests/browser_smoke.py` (19 cases) and `tests/agent_security.py` (40 cases, including live Claude + Codex turns) make future regressions catchable in <60s.
- **Codex provider parity work** — skill injection, chat history replay, substrate parity (snapshots + patches + alias bookkeeping at per-turn granularity), `start.py` launcher with persistence, model-not-supported auto-retry, activity strip mapping.
- **Pristine unification** — removed the separate `examples/_pristine/` directory; the ship-with-vs-current model is now collapsed to a single canonical `.md` per doc plus `.history/` (oldest snap = history-0). `↺ Reset` restores from history-0 instead of a sibling pristine file.
- **Provider runtime abstraction** — `agent_runtime/` registry, Protocol-based seam, Codex CLI adapter (experimental).
- **v0.1 public release** — adaptive reading (translate / restyle / expand); live in-place DOM patching; click-to-focus + multi-select; version history with restore; multi-view tabs (Doc / Graph / Source / LaTeX / Print); figure programs (canvas / SVG / Desmos); KaTeX math with `\begin{aligned}` rule; AMS-TeX importer; single-file browser deployment; per-doc model selector.
