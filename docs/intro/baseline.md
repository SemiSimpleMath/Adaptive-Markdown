---
doc_id: d-01KRJ4KNMZ0B3QJM
title: Welcome to Adaptive Markdown
audience: novice
language: en
---

# Welcome to Adaptive Markdown

This is a regular markdown file. What makes it *adaptive* is the chat panel on the right — the agent there can read, write, and edit this very file in place. The page you're looking at re-renders as the source changes. You can also edit it directly: click into any paragraph and start typing.

This document is itself the tutorial. Each section below ends with something to try. Click a block to give the agent focus on it (the dropdown caret in the chat header confirms the selection), then send your message.

## Definition (Adaptive document) {#adoc}

An **adaptive document** is a source file the reader can steer. Instead of a single, frozen render, the reader can ask the agent to expand sections, change the audience level, translate the prose, illustrate equations, or query the relationships between different parts of the document. Or just click in and edit by hand.

## Try 1 — Rewrite for a kid {#try-rewrite}

A function $f : \mathbb{R} \to \mathbb{R}$ is **continuous at the point $a$** if for every $\varepsilon > 0$ there exists $\delta > 0$ such that for all $x$ satisfying $|x - a| < \delta$, we have $|f(x) - f(a)| < \varepsilon$.

> Click the paragraph above. Then ask the agent: **"Rewrite this for a 10-year-old."**

The agent receives the block's id and content as explicit context, so it knows exactly which paragraph to operate on. Watch the chat panel narrate the edit, then watch this paragraph re-render in place.

## Try 2 — Translate from French {#try-translate}

Évariste Galois, mort à vingt ans dans un duel obscur en 1832, a transformé l'algèbre moderne en quelques nuits d'écriture fébrile. La veille de sa mort, il rédigea à la hâte ses dernières découvertes — la théorie des groupes appliquée aux équations polynomiales — dans une lettre adressée à Auguste Chevalier. Ces pages, longtemps ignorées, contiennent les germes d'une révolution mathématique que ses contemporains ne comprirent qu'à grand-peine, des décennies après sa mort.

> Click the French paragraph. Then ask: **"Translate this to English."**

## Try 3 — Add a figure {#try-figure}

The **unit circle** is the set of points $(x, y)$ in the plane satisfying $x^2 + y^2 = 1$. Every point on the circle can be written as $(\cos\theta, \sin\theta)$ for some angle $\theta \in [0, 2\pi)$.

> Click this section. Then ask: **"Add a figure that draws the unit circle and animates a point tracing it as $\theta$ increases."**

The agent edits the source to add a `<figure>` block containing a `<canvas>` and the JavaScript that draws on it. Open the **Source** tab to see exactly what got inserted — no hidden framework, no component palette. Just markdown with embedded HTML and `<script>`.

## Try 4 — Edit by hand {#try-edit}

You don't have to go through the agent for small fixes. Click into this paragraph and start typing — fix a typo, rephrase a sentence, replace a word. When you click elsewhere, the edit saves through the same snapshot + validator pipeline the agent uses; the doc's history records it.

Editing is locked while the agent is mid-turn (only one writer at a time). If a turn drags on, type `/cancel` in the chat box to interrupt it and take over. Inline edit currently covers prose blocks — paragraphs and headings; for lists, tables, code, and HTML blocks, use the **Source** tab.

## Try 5 — Change the page itself {#try-page}

Here's the difference between this and ChatGPT Canvas or Claude Artifacts. Those tools let the agent rewrite text or fill in a sandboxed component. **This page lets the agent rewrite the page.** The agent's tool surface is the same one any web developer has — arbitrary HTML, CSS, and JavaScript inlined into the source. There is no allowed-component list. If you can express it in a `<script>` tag, the agent can ship it.

Try one of these (you don't need to click anything first — these are page-wide changes):

- **"Add a dark-mode toggle to the top of the page."**
- **"Animate every section heading so it slowly pulses."**
- **"Make every letter on the page fall to the bottom when my mouse touches it."**
- **"Add a small game in the corner that I can play while I read."**

> **Tip:** Haiku is fast and usually fine for small visual touches. For bigger coding jobs (a working game, a non-trivial animation, anything with state machines or careful layout) switch the model dropdown to **Sonnet** or **Opus** — the larger models produce cleaner code on the first try, which more than offsets their per-turn cost. You can swap mid-conversation; it just starts a fresh chat.

When the agent is done, open the **Source** tab and scroll to the bottom. You'll see a literal `<script>` block (or `<style>`) that the agent wrote. This page isn't a render of a doc — it *is* the doc, executing.

## Bringing things in {#workflow-import}

Drag a `.pdf` or `.tex` file onto the **+ Doc** button and Claude converts it to adaptive-markdown server-side in a few seconds. Headings, lists, math, tables, theorem environments — all preserved. Try it with anything from arxiv. Office formats (`.docx`, `.xlsx`, `.pptx`) convert locally via markitdown; no API key needed for those.

Drop an image, audio file, video, or CSV onto an *open* doc instead and it lands in that doc's `assets/` folder. The agent gets a system message telling it what arrived, ready to embed.

Third-party content works too. Ask the agent **"embed a YouTube tutorial about Galois theory after the French paragraph"** and the right `<iframe>` lands in the source. The viewer's sandbox is configured so YouTube, CodePen, Figma, Observable, and similar render and function inside the doc.

## Sending things out {#workflow-export}

Click **Export** at the top — you get a single `.html` file containing the rendered doc, AM's typography CSS, KaTeX from CDN, every `<script>` tag your doc contains, and every referenced image inlined as a data URI. Send it to anyone, they open it in a browser, no AM needed. AM is what *makes* the doc; the doc itself is portable.

<div class="pinned">

This block is author-locked. The agent may restyle and augment the surrounding tutorial, but the words inside `<div class="pinned">` blocks won't be rewritten.

Authors can choose what sections are non-editable. The entire document history is also carried, and the original document is the provenance.

</div>

## What's next

A doc that you and an agent can both edit — and that can be a PDF this morning, a tutorial this afternoon, and a single shareable HTML file tonight — is a different kind of artifact from a static file. The source stays plain markdown: readable, portable, version-controllable, openable in any editor. Both the agent and you reach into it the same way.

Some directions in play:

- Inline edit for lists, tables, and code blocks (currently prose-only — formatting edits go through Source view).
- Cross-doc references so a textbook chapter can link to figures, theorems, or sections in a sibling chapter.
- Slide-show / classroom modes derived from the same source.
- Real-time speech-to-doc capture for note-taking; photo-of-blackboard → LaTeX.
- Citation and bibliography handling.
- Lean / Coq proof scaffolding when the underlying tooling matures.

If you have a use case that doesn't fit yet, the agent is also good at hearing "I wish this could…" and making the case for what would have to change.
