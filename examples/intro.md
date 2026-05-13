---
title: Welcome to v2
audience: novice
language: en
---

# Welcome to v2

This is a regular markdown document. It opens cleanly in any markdown editor or viewer — no special tooling required to *read* it.

What makes it adaptive is the **skill** an agent loads alongside it, plus the viewer that renders it interactively.

## Definition (Adaptive document) {#adoc}

Un **document adaptatif** est un fichier source que le lecteur peut piloter. Au lieu d'un rendu unique et figé, le lecteur peut demander à l'agent d'étendre des sections, de changer le niveau du public, de traduire la prose, d'illustrer des équations ou d'interroger les relations entre les différentes parties du document.

## Example (Click and ask) {#ex-click}

Try clicking on this block in the viewer, then asking:

- "Rewrite this for a child."
- "Translate this to French."
- "Add a figure that illustrates the idea."

The agent has the block's id and content as explicit context, so it knows exactly what to operate on.

## Theorem (The point) {#point}

**Statement.** A small, well-defined skill plus a small set of markdown conventions is enough to make documents feel alive under a reader's steering.

**Proof.** This document is the proof. You're reading it. $\square$

## See also

- The [definition of adaptive documents](#adoc)
- The [worked example](#ex-click) above

::: figure { renderer=svg intent="A simple arrow diagram showing: source → agent → reader, with feedback loop from reader to agent." }
:::

::: pinned
This block is author-locked. The agent may restyle the surrounding prose but must not modify the words inside this directive.
:::
