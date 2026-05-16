# The Adaptive Markdown format

This is the technical specification of the format. If you just want to write or read docs, [the tutorial](examples/intro.md) is enough. This file is for people building tooling, agents, viewers, or extensions.

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

::: figure { intent="Plot of f with horizontal tangent at c" renderer=canvas }
<canvas id="rolle-plot" width="640" height="280"></canvas>
<script>
  (function() { /* canvas drawing */ })();
</script>
:::
```

Four layers, each plain markdown:

1. **YAML frontmatter** delimited by `---` at top of file.
2. **Markdown body** — CommonMark with extensions noted below.
3. **Pandoc-style heading attributes** — `{#id .class key=value}` after a heading.
4. **`:::` directives** — fenced blocks with a name and optional attributes.

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

**Minting is lazy** — only when something needs to refer to a block precisely (agent edit, reader-click-to-focus). Don't sprinkle them everywhere.

**Preservation** — agents must never delete existing tracking IDs unless explicitly asked. On block split, the original ID stays with the first piece; the new sibling gets a fresh one. On merge, one ID survives; the dropped ones go to the alias map (see Sidecar files below).

## Directives

`:::` fenced blocks, name + optional pandoc-attrs:

```markdown
::: figure { intent="..." renderer=canvas }
<canvas></canvas>
<script>...</script>
:::

::: pinned
This block is author-locked.
:::

::: note { intent="Reminder of EVT" }
The Extreme Value Theorem says ...
:::
```

The viewer wraps directive bodies in `<div class="directive {name}">`. The CSS themes a few names by default:

| Directive | Built-in style |
|---|---|
| `figure` | Dashed border, centred — for canvas / SVG / image with optional intent description |
| `pinned` | Blue tint, left border — author-locked content the agent must not rewrite |
| `note`, `aside` | Muted background, left border — for callouts |

Any other directive name (`::: warning`, `::: example-output`, `::: my-thing`) renders as `<div class="directive my-thing">` — works fine, just no built-in theme. Add your own CSS in a doc-local `<style>` block.

### The `intent` attribute

For `:::figure` and other generative blocks, the `intent="..."` attribute is a human-readable description of what the figure shows. It serves two purposes:

1. **Accessibility** — used as alt text / aria-label.
2. **Regenerability** — when the agent is asked to "redo this figure with axes labelled", the intent describes what the figure is supposed to be, separately from the implementation.

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

The viewer renders each doc inside a sandboxed `<iframe>`. The iframe has its own `<html>`, `<head>`, `<body>`, JS context, localStorage, and event listeners. Anything you embed in the source — `<style>`, `<script>`, `<canvas>`, `<svg>`, custom HTML — runs inside that sandbox.

What this means in practice:

- `body { background: black; color: white; }` inside a `<style>` block paints **only this doc**, not the viewer chrome.
- `document.addEventListener('mousemove', ...)` inside a `<script>` only sees events inside the doc.
- `position: fixed; top: 0; right: 0;` anchors to the iframe viewport, not the page viewport. Floating UI stays inside the doc.
- `localStorage`, `sessionStorage`, `fetch`, `requestAnimationFrame` all work as in any web page.

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
| Directives | `figure`, `pinned`, `note`, `aside` themed | Any other name renders unstyled |
| Anchor IDs | Pattern `[\w-]+` | Any string in that pattern |
| Tracking IDs | Pattern `b-[A-Z0-9]+` | Minted by the system, not user-typed |
| `doc_id` | Pattern `d-[A-Z0-9]+` | Minted automatically |
| Embedded HTML | `<style>`, `<script>`, `<canvas>` etc. | Any HTML the iframe accepts |

There is no `--strict` linter today. The format is deliberately loose at v0.1 to let conventions emerge from real use. A schema-based check is on the roadmap.

## Sidecar files

The viewer maintains three sidecars next to each doc. None are part of the source — they're system state. All are gitignorable.

| Path | Purpose |
|---|---|
| `.history/<stem>/snap-{id}.md` | Pre-edit snapshots. Captured automatically by the pre-tool-use hook before any `Edit`/`Write`, plus a history-0 snapshot at backend startup for any doc that doesn't already have one. Browse-and-restore via the `↶ History` button; `↺ Reset` restores from the oldest snap-* (i.e. history-0). |
| `<doc>.id-aliases.json` | Union-find map of dropped/merged tracking IDs → their current surviving ID. Maintained on block merge / delete. Format: `{ "b-OLD": "b-NEW", "b-OLDER": "b-OLD" }`. Path-compressed on traversal. |
| `<doc>.patches/p-{id}.json` | Derived patches — each captures `{ts, parent, author, ops}` where ops are block-level `replace`/`insert`/`delete` with `before_hash` and `after_hash`. Lets us do granular conflict detection and future 3-way merges. |

## File-level invariants the system enforces

A few invariants the backend hooks maintain. Worth knowing if you're building tooling against the substrate:

- Every `.md` under `examples/` has a `doc_id` in its frontmatter (minted at first sight if missing).
- Every `.md` under `examples/` and `docs/` has at least one snapshot under `.history/<stem>/` (history-0 minted at first sight if missing).
- Every `Edit`/`Write` to a `.md` is preceded by a snapshot to `.history/`.
- Every `Edit`/`Write` to a `.md` produces a derived patch under `<doc>.patches/` if any tracking-ID-anchored blocks changed.
- Tracking IDs that disappear from a doc are recorded as tombstones in the alias map.

## Skill — the agent's contract

Everything above is the format. The agent's behaviour on top of the format is specified in [`.claude/skills/adaptive-markdown/SKILL.md`](.claude/skills/adaptive-markdown/SKILL.md) — ~350 lines of plain text the agent loads at the start of every session. The skill is the format's *normative* document for agent implementations; this file is the *descriptive* one for humans.

## Versioning

This document describes the format as of `v0.1`. Breaking changes will bump a `format_version` key in frontmatter (not yet present). For now, treat the format as evolving — pin to a specific git tag if you're building tooling against it.
