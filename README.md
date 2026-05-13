# v2 — adaptive markdown documents

A reader-steered markdown document system. You write a `.md` file with light conventions; an agent (loaded with a small skill) edits, expands, restyles, translates, illustrates, and queries it; the viewer lets readers click any block and ask anything about it.

**Status:** v2 rebuild from scratch. v1 lessons captured in `LESSONS_FROM_V1.md` (forthcoming). v1 lives at `E:\DPF\DPF\` and is referred to only for the skill content and the example documents we want to keep demoing.

## Design principles (lessons from v1)

1. **Markdown source, no new format.** A `v2.md` file is a regular markdown file. Open it in any editor, render it with any markdown viewer — it works. The "adaptive" lives in the skill + viewer, not the file extension.
2. **Skill is the artifact.** ~300 lines of plain text that teaches any agent how to operate on the document. The skill is portable: same text works in Claude Code CLI, in the embedded SDK, in the browser viewer.
3. **Markdown directives instead of HTML tags.** Use `:::figure { intent="..." renderer=canvas }` instead of `<ddf-figure ...>`. No HTML-in-markdown parser fights. Pandoc / MyST / remark-directive all support this.
4. **Browser-first deployment.** The primary artifact is a single `index.html` you can open from any file system. Optional native backend exists for users who want real-filesystem-and-bash for Lean/Python integration.
5. **AST-based pipeline.** Parse once (markdown-it + plugins), render many. No regex soup. The same AST drives the doc view, the graph view, the LaTeX export, etc.
6. **Components register themselves.** Each block type lives in a single file declaring parse rules + HTML render + LaTeX render + DAG-extract + skill snippet. Adding a new block kind is one file, not edits across the codebase.
7. **Views are first-class.** A view is `(name, generator)`. Built-in: doc, graph, source, latex, print. New views slot in by registering a function.
8. **In-place DOM patching.** When the agent edits, the viewer fetches the new compiled HTML, diffs against the current DOM, applies surgical replacements. No iframe reloads, no scroll jumps.

## Folder layout (planned)

```
v2/
├── README.md                # this file
├── SKILL.md                 # the skill — the agent's contract
├── LESSONS_FROM_V1.md       # what worked, what didn't, why we're rebuilding
├── index.html               # single-file browser app
├── viewer.py                # optional native backend (later)
├── components/              # one file per block type
│   ├── theorem.js
│   ├── proof.js
│   ├── figure.js
│   ├── computation.js
│   ├── pinned.js
│   └── claim.js
└── examples/
    ├── intro.md             # tiny smoke test
    ├── textbook.md          # ported from v1's MVT doc
    └── paper.md             # ported from v1's Apéry conversion
```

## Capabilities (target functionality)

Carried over from v1 (proven):
- Edit / expand / collapse / restyle / translate any block via chat
- Click-to-focus selection (single + multi-select via shift/ctrl)
- Reserved heading words → structural blocks (Theorem, Lemma, Definition, Proof, Example, Remark)
- Dependency graph view derived from cross-reference links
- Multiple derived views from a single source (HTML, Graph, LaTeX, Print, Source)
- Per-doc version history with browse-and-restore
- Live in-place DOM patches on agent edits
- Figure intent-vs-implementation pattern

New in v2:
- Annotations layer (highlights, query results) — non-destructive overlay
- Dependency-aware operations ("expand this proof, plus the lemmas it cites")
- Audience presets — reader picks novice/intermediate/expert; cached per-doc render
- Live computation via Pyodide (in-browser Python / SymPy)
- Chat-history-per-block — clicking a block shows the conversation that shaped it
- Component plugin system — new block types as drop-in files

Deferred / stretch:
- Lean verification of formal statements
- Multi-document workspace + cross-doc dependency graph
- Multi-user real-time collaboration
