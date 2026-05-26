# The Adaptive Markdown format

This is the technical specification of the format. If you just want to write or read docs, [the tutorial](docs/intro/baseline.md) is enough. This file is for people building tooling, agents, viewers, or extensions.

## Design invariant

An adaptive markdown file is a plain `.md` file. Open it in any text editor; render it with any CommonMark-compatible viewer; commit it to any VCS. It works. The "adaptive" part lives in **the agent's skill** ([`SKILL.md`](.claude/skills/adaptive-markdown/SKILL.md)) and **the viewer** ([`index.html`](index.html)) — not in the file extension or a parser fork.

This is the single biggest design decision and it constrains everything else. If a feature would require a private syntax that breaks plain markdown rendering elsewhere, it doesn't ship.

## File shape

```markdown
---
doc_id: d-01KRJ4KNMZ0B3QJM
title: The Mean Value Theorem
authors: ["Strang", "Herman"]
audience: novice
language: en
---

# The Mean Value Theorem

## Definition (Continuous function) {#cts}

A function $f$ is *continuous* at $a$ if ...

<!-- id:b-01KRJC5M8QZDV5DQ -->
## Theorem (Rolle's Theorem) {#rolle}

**Statement.** Let $f$ be continuous on $[a,b]$ ...

**Proof.** ... $\square$

<figure>
<canvas id="rolle-plot" width="640" height="280"></canvas>
<script>
  (function() { /* canvas drawing */ })();
</script>
<figcaption>Plot of f with horizontal tangent at c.</figcaption>
</figure>
```

Four layers, each plain markdown (CommonMark allows raw HTML blocks — the last layer is just HTML passed through):

1. **YAML frontmatter** delimited by `---` at top of file.
2. **Markdown body** — CommonMark with extensions noted below.
3. **Pandoc-style heading attributes** — `{#id .class key=value}` after a heading.
4. **HTML blocks** for structured content — `<aside class="note">`, `<figure>`, `<section class="theorem">`, `<div class="pinned">`, plus `<style>` / `<script>` / `<svg>` / `<canvas>` for anything interactive.

## Frontmatter

YAML between two `---` lines, must start at line 1. The viewer reads these keys; everything else is passed through unchanged:

| Key | Purpose |
|---|---|
| `doc_id` | Stable document identifier. Format: `d-` + 16-char base32 ULID-truncation. Minted automatically at first save if missing. |
| `title` | Document title. Used for browser tab and metadata. The body's `# H1` is the visible page title. |
| `authors` | Single string or YAML list. Shown in the meta line under the title. |
| `audience` | Free-form. Common values: `novice`, `intermediate`, `expert`. Shown in meta line. |
| `language` | BCP-47 tag (`en`, `fr`, `de`, etc.). Shown in meta line. |
| `source_url`, `fetched`, `license` | Optional provenance fields (used by import flow). |

Add any other keys you want — the viewer ignores them, the agent can read them.

## Reserved heading words

The viewer recognises a small set of words as the **first word of a heading**, and annotates the rendered `<h1>`-`<h6>` with `data-kind="..."`. The CSS in the viewer (and inside the iframe srcdoc) themes each kind:

| Word | Theming |
|---|---|
| `Theorem`, `Lemma`, `Proposition`, `Corollary` | Italic, blue (`#2a4d7a`) |
| `Definition` | Italic, gold (`#c39548`) |
| `Example`, `Solution`, `Proof` | Italic, green (`#6a9255`) |
| `Remark`, `Note`, `Aside` | Italic, muted grey |

Match is case-insensitive on the first word only. Anything after — including parenthetical names — is free:

```markdown
## Theorem (Rolle's) {#rolle}
## Definition (Continuous function) {#cts}
## Example 3.2 — Polynomial case
```

If the first word isn't in the set, no `data-kind` is added and the heading renders unadorned. **The set is not enforced** — using `## Proof.` works exactly the same as `## proof — finishing the case`.

## Two-tier identity model

Two kinds of IDs, with very different lifecycles:

### Anchor IDs (`{#id}`)

Human-meaningful, used for cross-references. Author-controlled.

```markdown
## Theorem (Rolle's) {#rolle}

By [Rolle's theorem](#rolle), there exists $c$ with $f'(c) = 0$.
```

Markdown-it-attrs parses these. Pandoc, MyST, GitHub, and most tooling does too — so the source stays portable. Anchor IDs can be anything matching `[\w-]+`.

### Tracking IDs (`<!-- id:b-... -->`)

System-minted, invisible, used for the patch/alias substrate. The tracking comment immediately preceding a block (separated only by blank lines) **is** that block's tracking ID:

```markdown
<!-- id:b-01KRJC5M8QZDV5DQ -->
## Theorem (Rolle's) {#rolle}
```

Format: `b-` + 16-char base32 ULID-truncation. Time-sortable (first 10 chars), 30 bits of randomness. Renders as an HTML comment in every markdown viewer — completely invisible.

**Minting is eager and system-maintained.** Whenever the runtime writes `current.md` (import, agent turn, inline edit, restore, startup backfill) it stamps a tracking ID before every top-level **heading** and **paragraph** that lacks one, so any block can be located by a stable identity instead of by fuzzy text matching. Fenced code, `<script>` / `<style>` / `<pre>` bodies, lists, tables, and blockquotes are left unstamped — a comment inside them would be unsafe (it can break a fence or fail JS validation) or pointless. The agent never hand-authors these IDs; the system does, and the agent only preserves them. Because they're HTML comments they stay invisible in every renderer and are dropped by every converter, so the source still degrades gracefully to clean markdown.

**Preservation** — agents must never delete existing tracking IDs unless explicitly asked. On block split, the original ID stays with the first piece; the new sibling gets a fresh one. On merge, one ID survives; the dropped ones go to the alias map (see Sidecar files below).

## Structured content via HTML blocks

CommonMark passes HTML blocks through verbatim. The viewer applies CSS by class name. This means structured content (callouts, figures, locked sections, theorems) is written as plain HTML in the source — no parallel grammar to learn, no plugin to coordinate with.

The vocabulary the viewer themes by default:

| Pattern | What it is | Renders as |
|---|---|---|
| `<aside class="note">…</aside>` | Callout — also `class="aside"` or `class="remark"` | Muted background, left border |
| `<div class="pinned">…</div>` | Author-locked content the agent must not rewrite | Blue tint, left border, 🔒 chip |
| `<figure>…<figcaption>…</figcaption></figure>` | Figure with optional caption | Centred; dashed placeholder border when empty |
| `<section class="theorem" id="rolle">…</section>` | Explicit-boundary kind-block — also `lemma`, `proposition`, `corollary`, `definition`, `example`, `proof`, `solution`, `abstract` | Coloured left border for boundary; heading inside picks up `data-kind` styling |

The class vocabulary is **open** — adding a new semantic class (`<aside class="caveat">`, `<section class="conjecture">`) costs zero parser changes. Add a single CSS rule in a doc-local `<style>` block, or define it in the viewer's baseline for project-wide use.

### Inline markdown inside HTML blocks

To embed markdown *inside* an HTML block, surround the inner content with blank lines:

```html
<aside class="note">

This **inner text** renders as markdown, not raw HTML.

</aside>
```

Without the blank lines, the content is treated as raw HTML. This is CommonMark behaviour, not an AM-specific rule.

### Figures

A `<figure>` with no rendered content (just a `<figcaption>` describing intent) shows the placeholder border, signalling "this figure hasn't been drawn yet." Once the body contains a `<canvas>`, `<svg>`, `<img>`, etc., the placeholder border goes away. The caption stays visible — that's HTML5 semantic intent.

For attribution / accessibility, `aria-label="..."` on the `<figure>` is honoured. For generative figures the agent should be able to redraw later, write a concrete `<figcaption>` describing what the figure shows; the regenerability hint lives in the caption text itself.

## Math

Use standard `$...$` (inline) and `$$...$$` (display) delimiters. The viewer renders via [KaTeX](https://katex.org).

**Multi-line equations:** prefer `\begin{aligned}...\end{aligned}` inside `$$`. Avoid `eqnarray` and bare `align` — they don't round-trip cleanly through KaTeX:

```markdown
$$
\begin{aligned}
f'(c) &= \frac{f(b) - f(a)}{b - a} \\
       &= \text{average rate of change}
\end{aligned}
$$
```

**Long equations with `\tag{N}`** — KaTeX absolute-positions the tag at the right margin. In a 760px-wide reading column, the tag will collide with long content. Use `\begin{aligned}` to wrap-and-align so the equation fits inside the column.

## Embedded `<style>` and `<script>`

The viewer renders each doc inside a sandboxed `<iframe>` loaded from a **different origin than the parent viewer** (e.g. `localhost:<port>` while the viewer is on `127.0.0.1:<port>`). The sandbox is `allow-scripts allow-popups allow-same-origin`: `allow-same-origin` is safe here because the iframe is same-origin to *itself* (so YouTube / CodePen / Observable embeds bootstrap), but the cross-origin boundary still blocks any access to the parent's DOM, storage, or cookies. The iframe has its own `<html>`, `<head>`, `<body>`, JS context, and event listeners. Anything you embed in the source — `<style>`, `<script>`, `<canvas>`, `<svg>`, custom HTML — runs inside that sandbox.

What this means in practice:

- `body { background: black; color: white; }` inside a `<style>` block paints **only this doc**, not the viewer chrome.
- `document.addEventListener('mousemove', ...)` inside a `<script>` only sees events inside the doc.
- `position: fixed; top: 0; right: 0;` anchors to the iframe viewport, not the page viewport. Floating UI stays inside the doc.
- `requestAnimationFrame`, `setTimeout`, `MutationObserver`, canvas/SVG drawing APIs, `Web Audio`, etc. all work as in any web page.
- `localStorage` and `sessionStorage` **work**, scoped to the iframe's own origin (`localhost:<port>`). State survives across doc reloads within the same browser session. Use for per-doc preferences ("remember dark mode"). The cross-origin boundary keeps doc storage isolated from the parent viewer's storage, so a doc can't read viewer preferences and vice versa.
- `fetch` works for CDN URLs and same-origin paths (the iframe's `<base href="/docs/<slug>/">` lets `fetch("assets/data.csv")` resolve correctly). Cross-origin reads to the viewer's parent origin (`127.0.0.1:<port>` from `localhost:<port>`) are CORS-blocked — you can't reach the viewer's backend from inside the doc.

The doc is, literally, a self-contained webpage. Hand the `.md` file to someone else and they get your dark mode, your falling letters, your custom UI — all stored *in* the doc.

## Cross-references

Standard markdown link syntax against an anchor ID:

```markdown
By [Rolle's theorem](#rolle), there exists $c$ ...
```

The viewer's **Graph** view builds a DAG from these cross-references: nodes are blocks with reserved-word headings and anchor IDs; edges are intra-doc anchor links.

For **typed citations** (planned, see roadmap), the syntax extends to:

```markdown
[Apéry's proof](paper-Apery1978#main){type=depends}
```

The `{type=...}` attribute classifies the relationship (depends, mentions, generalizes, supersedes, ...) for cross-doc dependency graphs. Today this attribute is ignored by the viewer; the multi-doc workspace will read it.

## What's fixed vs free

| Layer | Fixed | Free |
|---|---|---|
| Frontmatter | Keys the viewer reads (table above) | Any other key |
| Headings | Reserved first words → `data-kind` | Any other first word |
| Themed classes | `note`, `aside`, `remark`, `pinned`, kind-block names (`theorem`, `lemma`, …) | Any other class — renders unstyled unless you supply CSS |
| HTML blocks | CommonMark rules (open at start of line, blank lines around inline markdown) | Any HTML the iframe accepts |
| Anchor IDs | Pattern `[\w-]+` | Any string in that pattern |
| Tracking IDs | Pattern `b-[A-Z0-9]+` | Minted by the system, not user-typed |
| `doc_id` | Pattern `d-[A-Z0-9]+` | Minted automatically |

The format is deliberately loose. There is no `--strict` linter today; the validator only checks JS/CSS/SVG syntax inside embedded blocks, not the HTML structure. The browser is forgiving of malformed HTML, so the failure mode is visual glitch rather than hard error.

## Layout — doc as folder

Each doc is a self-contained folder under `docs/`:

```
docs/
└── <slug>/
    ├── baseline.md       # immutable history-0 (tracked in git for ship-with docs)
    ├── current.md        # working copy the agent edits (gitignored)
    ├── original.<ext>    # optional provenance (the .tex, .pdf, etc. the doc came from)
    ├── snaps/            # pre-edit snapshots (gitignored)
    │   └── snap-{id}.md
    └── assets/           # materials the doc embeds — figures, audio, data files
```

The slug is the doc's canonical identifier in the WebSocket protocol and in the file picker. `baseline.md` is the Reset target — the doc as it was before any agent ever touched it. `current.md` is the file the agent edits; gitignored so live testing never leaks into the published artifact. `snaps/` accumulates pre-edit snapshots; the History panel reads from here.

Each folder is the unit of sharing — zip `docs/<slug>/` and you have a portable doc plus its history and provenance.

### Sidecars

| Path | Purpose |
|---|---|
| `docs/<slug>/baseline.md` | Immutable history-0. Tracked in git for ship-with docs. The Reset button restores from here. |
| `docs/<slug>/snaps/snap-{id}.md` | Pre-edit snapshots. Captured automatically by the pre-tool-use hook before any agent `Edit`/`Write`, plus history-0 mint at startup if a doc has none. Browse-and-restore via the History panel. |
| `<doc>.id-aliases.json` | Union-find map of dropped/merged tracking IDs → their current surviving ID. Maintained on block merge/delete. Format: `{ "b-OLD": "b-NEW" }`. Path-compressed on traversal. |

## File-level invariants the system enforces

A few invariants the backend hooks maintain. Worth knowing if you're building tooling against the substrate:

- Every `docs/<slug>/current.md` has a `doc_id` in its frontmatter (minted at first sight if missing).
- Every doc folder has a `baseline.md` (the Reset target). On fresh clones, the backend copies `baseline.md` → `current.md` if `current.md` is missing.
- Every agent `Edit`/`Write` to `current.md` is preceded by a snapshot to `snaps/`.
- The only path the agent may write to is `docs/<slug>/current.md`. `baseline.md`, `snaps/`, and everything outside `docs/` are off-limits; the PreToolUse hook rejects attempts with a clear error.
- Tracking IDs that disappear from a doc are recorded as tombstones in the alias map.

## Skill — the agent's contract

Everything above is the format. The agent's behaviour on top of the format is specified in [`.claude/skills/adaptive-markdown/SKILL.md`](.claude/skills/adaptive-markdown/SKILL.md) — a self-contained spec the agent loads at the start of every session. The skill is the format's *normative* document for agent implementations; this file is the *descriptive* one for humans.

## Versioning

This document describes the format as of `v0.1`. Breaking changes will bump a `format_version` key in frontmatter (not yet present). For now, treat the format as evolving — pin to a specific git tag if you're building tooling against it.
