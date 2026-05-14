---
doc_id: d-01KRJ4KNMZ0B3QJM
title: Welcome to Adaptive Markdown
audience: novice
language: en
---

# Welcome to Adaptive Markdown

**This document is a programmable object.** It's not a render of a file, it *is* the file, executing. Any document is now just your launching pad! The coding agent on the right, turns *"I wonder if…"* into a live, working page. Rewrite a paragraph, add a figure, ELI5, complete this proof, create dark mode, animate the headings, drop in a game — all by asking.

Each section below ends with something to try. Click any block to give the agent focus on it, then send your message.

## Try 1 — Rewrite for a kid {#try-rewrite}

A function $f : \mathbb{R} \to \mathbb{R}$ is **continuous at the point $a$** if for every $\varepsilon > 0$ there exists $\delta > 0$ such that for all $x$ satisfying $|x - a| < \delta$, we have $|f(x) - f(a)| < \varepsilon$.

> Click the paragraph above. Then ask the agent: **"Rewrite this for a 10-year-old."**

The agent receives the block's id and content as explicit context, so it knows exactly which paragraph to operate on. Watch the chat narrate the edit, then watch this paragraph re-render in place.
    
## Try 2 — Translate from French {#try-translate}

Évariste Galois, mort à vingt ans dans un duel obscur en 1832, a transformé l'algèbre moderne en quelques nuits d'écriture fébrile. La veille de sa mort, il rédigea à la hâte ses dernières découvertes — la théorie des groupes appliquée aux équations polynomiales — dans une lettre adressée à Auguste Chevalier. Ces pages, longtemps ignorées, contiennent les germes d'une révolution mathématique que ses contemporains ne comprirent qu'à grand-peine, des décennies après sa mort.

> Click the French paragraph. Then ask: **"Translate this to English."**

## Try 3 — Add a figure {#try-figure}

The **unit circle** is the set of points $(x, y)$ in the plane satisfying $x^2 + y^2 = 1$. Every point on the circle can be written as $(\cos\theta, \sin\theta)$ for some angle $\theta \in [0, 2\pi)$.

> Click this section. Then ask: **"Add a figure that draws the unit circle and animates a point tracing it as $\theta$ increases."**

The agent edits the source to add a `:::figure` block containing a `<canvas>` and the JavaScript that draws it. Open the **Source** tab to see exactly what got inserted — no hidden framework, no component palette. Just markdown with an embedded `<script>`.

## Try 4 — Change the page itself {#try-page}

Most agent-driven editors restrict the agent to a fixed set of components or a constrained widget palette. **This page lets the agent rewrite the page itself.** The agent's tool surface is the same one any web developer has — arbitrary HTML, CSS, and JavaScript inlined directly into the source. No allowed-component list. If you can express it in a `<script>` tag, the agent can ship it.

Try one of these (no click needed — these are page-wide changes):

- **"Add a dark-mode toggle to the top of the page."**
- **"Animate every section heading so it slowly pulses."**
- **"Make every letter on the page fall to the bottom when my mouse touches it."**
- **"Add a small game in the corner that I can play while I read."**

If you don't like what the agent creates, you can always revert the change or instruct it again until you get what you want.

Open the **Source** tab afterward and scroll to the bottom — you'll see the literal `<script>` block the agent wrote. The page isn't a render of a doc; it *is* the doc, executing.

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

