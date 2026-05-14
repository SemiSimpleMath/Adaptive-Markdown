---
name: adaptive-markdown
description: Use when reading, writing, or editing a Markdown (.md) document that follows the adaptive-markdown conventions — a small set of heading patterns, in-block labels, and `:::` directives that let a reader steer the document interactively (expand, restyle, translate, illustrate, query). Triggers on .md files in a project where this skill is loaded.
---

# Adaptive Markdown

A markdown document the reader can steer. The reader clicks any block, asks the agent to expand / restyle / translate / illustrate / query it, and the doc updates in place. The source is plain markdown — opens cleanly in any editor, renders sensibly in any markdown viewer.

This skill is the agent's contract. Without it, an agent doesn't know the conventions and can't operate on the document predictably.

## Your domain

**You are operating on one markdown document. The reader is viewing the rendered version of it right now. Your work lives in that view — nowhere else.**

- When the reader says "this page," "the screen," "the doc," "here," "this," they mean the active `.md` file's rendered view.
- For anything visual, interactive, animated, or computed, **edit the `.md` source** to add it. The render updates automatically.
- Don't create sidecar `.html` files for effects, demos, or visualizations. The document IS your canvas. If you reach for `Write` on a new `.html` file, stop — almost always the right move is to `Edit` the active `.md` and add a `:::figure` directive with `<script>` body inside.
- Tools that genuinely don't belong in the document (build scripts, preprocessors, one-off CLI utilities) live in the project root and you should call that out explicitly when creating them. Default assumption: the request is about the doc.

The answer to "make X happen on this page" is almost always **Edit the active `.md` file to add the implementation**, then let the viewer's in-place patch logic re-render.

## Mental model

- **Source** (`*.md`): the truth. Frontmatter + markdown body + a tiny set of `:::` directives.
- **Render**: HTML produced live in the browser by the viewer. Math via KaTeX. Theorem-like styling via CSS classes derived from heading words.
- **Annotations** (`*.annotations.json`, optional): non-destructive overlay (highlights, query results). Never mutates the source.

You edit the **source**. The render is derived.

## File shape

```markdown
---
title: "The Mean Value Theorem"
authors: ["Strang", "Herman"]
audience: novice
language: en
---

# The Mean Value Theorem

## Definition (Continuous function) {#cts}

A function $f$ is *continuous* at $a$ if ...

## Theorem (Rolle's Theorem) {#rolle}

**Statement.** Let $f$ be continuous on $[a,b]$ ...

**Proof.** Let $k = f(a) = f(b)$. We consider three cases:

*Case 1.* If $f(x) = k$ throughout, then $f'(x) = 0$.

*Case 2.* By the [definition of continuity](#cts), ... $\square$

::: figure { intent="Plot of f with horizontal tangent at c" renderer=canvas }
<canvas id="rolle-plot" width="640" height="280"></canvas>
<script>
  (function() { /* canvas drawing */ })();
</script>
:::
```

## Reserved heading words

When the **first word** of a heading is one of these (case-insensitive), the heading marks a structural block. The CSS gives each kind a visual treatment (boxed background, left border, color).

| Heading word(s) | Block kind |
|---|---|
| Theorem, Lemma, Proposition, Corollary | Numbered named statement |
| Definition | Numbered definition |
| Proof | Argument body |
| Example | Worked example |
| Solution | Solution to an example |
| Remark, Note, Aside | Inline aside |
| Abstract | Paper abstract |

Heading patterns:

- `## Theorem (Pythagoras) {#pythagorean}` — named, explicit id
- `## Theorem 2.1 (Pythagoras)` — explicit number plus name
- `## Lemma 1` — numbered, unnamed
- `## Definition {#cts}` — unnamed, explicit id

If `{#id}` is absent, the viewer auto-generates one from heading text — but for stability of cross-references, prefer explicit ids on anything you might cite later.

**Cross-references** use standard markdown links: `[Theorem 2.1](#pythagorean)`.

## In-block labels

Inside a block, bold/italic patterns mark sub-structure:

- `**Statement.**` — formal statement of a theorem (optional)
- `**Proof.**` — proof body inline within a theorem block
- `**Solution.**` — solution body inline within an example
- `*Case 1.*`, `*Case 2.*`, `*Part a.*` — italic sub-structure markers
- End of proof: `$\square$`

## Directives (`:::` blocks)

Use directives for things markdown can't express natively. Each directive is `:::name { key=value key=value }` ... `:::`. Three are reserved:

- **`::: figure { intent="..." renderer=svg|canvas|three|desmos }`** — A figure. With body content, the body is the implementation (canvas+script, svg, Desmos calculator setup). Without body content (or just description), the directive renders as a placeholder showing the intent text.
- **`::: pinned`** — Author-locked content. You must not edit text inside this directive. May restyle surrounding prose but never the wrapped content.
- **`::: computation { lang=python|js|sympy }`** — Code + result. Re-runnable on demand.

You may also use directives instead of headings for structural blocks if you want explicit boundaries:

```
::: theorem { id="rolle" name="Rolle's Theorem" }
Let $f$ be continuous on $[a,b]$ ...
:::
```

Heading form and directive form are equivalent. Mix freely. Heading form reads better in plain markdown viewers; directive form is unambiguous about boundaries.

## Author intent

- **`::: pinned`** — preserve content verbatim. Agent may restyle the surroundings but not the wrapped text.
- **Default (unmarked content)** — expandable. Agent may rewrite, expand, collapse, translate.

## Response taxonomy

Reader requests fall into four buckets. Decide first, then act.

| Bucket | Examples | Action |
|---|---|---|
| **Source edit** | "Expand this proof", "Translate to French", "Add a figure", "Restate for beginners" | Edit the `.md` source. The viewer re-renders. |
| **Annotation** | "Highlight where assumption X is used", "Mark steps that depend on continuity" | Append to `*.annotations.json` (sidecar). Don't touch source. |
| **Query** | "Why is this assumption key?", "What depends on lemma 2?" | Read source, traverse id/link relations, explain in chat. No file edits. |
| **Conversation** | "I don't follow", "What's the motivation?" | Reply in chat. No file edits. |

Ambiguous → default to Query, offer to edit or annotate.

## Editing conventions

- **Use Edit, not Write**, for existing `.md` files. Write only for new files.
- **Preserve ids.** Heading ids and `::: directive { id="..." }` ids are cross-reference targets. Renaming breaks `[Theorem 2.1](#id)` links. If you must rename, update all incoming references in the same edit.
- **Preserve `::: pinned` content verbatim.**
- **Preserve YAML frontmatter** unless the user asks to change metadata.
- **Don't fabricate.** Don't invent lemmas, proof steps, or citations not present in the source. If a sketch's intent is unclear, ask before expanding.
- **Math notation.** Preserve `$...$` / `$$...$$` verbatim. Use `\tag{n}` for numbered equations.
- **Long display equations.** If a single display equation (`$$...$$`) is wider than roughly 70 characters of LaTeX source, **break it across lines using `\begin{aligned} ... \end{aligned}`**. The reader's column is ~760px wide and KaTeX's `\tag{n}` is absolutely-positioned at the right edge — when a one-line equation overflows the column, the tag collides with the equation's right side and produces visible garbling. Fix pattern:

  ```
  $$\begin{aligned}
  \text{long expression} &= \text{rhs part 1} \\
  &\quad + \text{rhs part 2}.
  \end{aligned} \tag{14}$$
  ```

  Place `\tag{n}` *outside* `\end{aligned}` (right before the closing `$$`) so it labels the whole aligned block, not a single row. Use `&` to align at `=` or another natural alignment point; `\\` for line breaks. When in doubt, break on the `=` sign — that's how mathematicians format wide equations on a page.

  When writing a new equation, prefer single-line form for short ones (most equations); reach for `\begin{aligned}` only when the source line exceeds ~70-80 characters or contains multiple terms that would overflow.

## Figures: intent vs. implementation

`::: figure` has two forms.

**Placeholder (no body or descriptive body only):**

```
::: figure { intent="A right triangle with squares on each side" renderer=svg }
:::
```

Use this when authoring and you haven't drawn it yet, or when explicitly asked only to describe.

**Implementation (body contains drawing code):**

```
::: figure { intent="Animation of Rolle's theorem ..." renderer=canvas }
<canvas id="rolle-anim" width="640" height="280"></canvas>
<script>
(function() {
  const ctx = document.getElementById('rolle-anim').getContext('2d');
  // ... drawing / animation ...
})();
</script>
:::
```

**Pick the implementation form when the reader asks you to *make / draw / illustrate / animate / plot / render*.** Don't leave a request like that as intent-only.

### Desmos figures (renderer=desmos)

The viewer loads Desmos's calculator API. To embed an interactive plot:

```
::: figure { intent="Plot y=(x-1)(x-3)+1 and its derivative" renderer=desmos }
<div id="rolle-plot" style="width:100%;height:380px;"></div>
<script>
(function() {
  if (!window.Desmos) return;
  const calc = Desmos.GraphingCalculator(document.getElementById('rolle-plot'));
  calc.setExpression({ id:'f',  latex:'y=(x-1)(x-3)+1' });
  calc.setExpression({ id:'df', latex:'y=2x-4', color:'#b34141' });
  calc.setExpression({ id:'c',  latex:'x=2', color:'#3ab83a' });
})();
</script>
:::
```

Use `renderer=desmos` when the figure is naturally a graph and the reader will want to pan/zoom/probe. The agent expresses curves and constraints as LaTeX inside `setExpression`.

### Conventions for implementation bodies

- Wrap scripts in an IIFE — never leak globals.
- Unique `id`s on containers (e.g. include the figure's purpose: `rolle-anim`, `mvt-secant`).
- Animations: `requestAnimationFrame`, never `setInterval`.
- No external `<script src=...>` libraries unless the page already loads them.
- Keep the figure inert if its math is wrong — never silently fake values.

## Parallel sub-work

When a request spans multiple independent sections (translate every theorem, expand every proof, restyle every example), delegate via the `Agent` tool to a section-worker subagent — one call per section. As soon as you have a result, Edit it in. Don't batch edits at the end.

When NOT to parallelize:
- Single-section operations.
- Operations where one section's outcome affects another (renumbering, cross-ref rewrites).
- Operations needing global consistency (whole-doc translation — better as one pass for terminology).

## See also

- `import-tex` skill (future) — converting LaTeX/AMS-TeX papers into adaptive markdown
- `export-tex` skill (future) — emitting LaTeX from adaptive markdown
- `claim-lean` skill (future) — formal-statement verification via Lean
