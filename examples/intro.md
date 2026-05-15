---
doc_id: d-01KRJ4KNMZ0B3QJM
title: Welcome to Adaptive Markdown
audience: novice
language: en
---

# Welcome to Adaptive Markdown

This is a regular markdown file. What makes it *adaptive* is the chat panel on the right — the agent there can read, write, and edit this very file in place. The page you're looking at re-renders as the source changes.

This document is itself the tutorial. Each section below ends with something to try. Click a block to give the agent focus on it (the dropdown caret in the chat header confirms the selection), then send your message.

## Definition (Adaptive document) {#adoc}

An **adaptive document** is a source file the reader can steer. Instead of a single, frozen render, the reader can ask the agent to expand sections, change the audience level, translate the prose, illustrate equations, or query the relationships between different parts of the document.

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

The agent edits the source to add a `:::figure` block containing a `<canvas>` and the JavaScript that draws on it. Open the **Source** tab to see exactly what got inserted — no hidden framework, no component palette. Just markdown with an embedded `<script>`.

## Try 4 — Change the page itself {#try-page}

Here's the difference between this and ChatGPT Canvas or Claude Artifacts. Those tools let the agent rewrite text or fill in a sandboxed component. **This page lets the agent rewrite the page.** The agent's tool surface is the same one any web developer has — arbitrary HTML, CSS, and JavaScript inlined into the source. There is no allowed-component list. If you can express it in a `<script>` tag, the agent can ship it.

Try one of these (you don't need to click anything first — these are page-wide changes):

- **"Add a dark-mode toggle to the top of the page."**
- **"Animate every section heading so it slowly pulses."**
- **"Make every letter on the page fall to the bottom when my mouse touches it."**
- **"Add a small game in the corner that I can play while I read."**

When the agent is done, open the **Source** tab and scroll to the bottom. You'll see a literal `<script>` block (or `<style>`) that the agent wrote. This page isn't a render of a doc — it *is* the doc, executing.

::: pinned
This block is author-locked. The agent may restyle and augment the surrounding tutorial, but the words inside `:::pinned` directives won't be rewritten.

Authors can choose what sections are non-editable. The entire document history is also carried, and the original document is the provenance.
:::

## Future
In a few years, no one will be reading journals on paper. Everyone will be interacting with articles, translating them instantly, exploring alternative proofs, asking questions, writing code on the spot into the document. This is Adaptive Markdown.

## More Use Cases (this is a growing list!)

- Embed images and automatically create descriptions of them and alt text.
- Embed audio and have it translated.
- Embed video and have the coding agent create playback features for you.
- Excel sheets, tabular data of any kind.
- Live consoles inside your document.
- Slide shows.
- Take notes in classrooms with automatically maintained formatting.
- Attach other agents and make this part of your workflow — have them format your documents, extract important parts, email documents, etc.
- Run executable code.
- Convert proofs to Lean (once reliable).
- Convert documents to any format.
- Record a person speaking in real time from your computer stream the text to your document, take pictures of a blackboard have the blackboard notes converted to LaTex.
