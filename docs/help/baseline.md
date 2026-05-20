---
doc_id: d-help
title: "Help"
audience: reader
---

# Adaptive Markdown — Help

This is a markdown viewer where the doc edits itself. Chat with the agent on the right — ask it to rewrite, restructure, illustrate, translate, plot — and watch the source change in place. The file stays plain `.md` and opens in any markdown viewer for read-only.

If you want to see this happen, open the **intro** doc from the dropdown at the top, click into a paragraph, and ask the agent to "rewrite this for a 10-year-old."

## Talking to the agent

Type a message and press **Enter**. The agent sees the full current source, any blocks you've selected, the current time, and your active doc.

- **Shift+Enter** — newline without sending
- **Esc** — clear selection / insertion point
- `/help` in chat — full slash-command list

## Selecting blocks

Click any paragraph, heading, figure, or list to **focus the agent on that block**. The block gets an outline; the chat header shows what's selected; your next message acts on it.

- **Shift+click** another block — multi-select
- **Alt+click** — escalate one level. Inside a `<details>` (collapsible week, say)? Alt+click selects the wrapping details. Useful when you want the agent to act on a whole section, not just one line. Repeat to walk further up.
- **Click in the gap** between two blocks — set an insertion point. The agent's next addition lands there.

## Editing prose directly

Click into any paragraph or heading and just start typing. Plaintext-only — your text replaces the prose, but bold / italic / math / links in the source survive the round-trip. For lists, code, or tables, edit via **Source view** (under **View ▾**) instead.

## The tabs

| Tab | What it does |
|---|---|
| **Doc** | Rendered view — the default. |
| **Review** | Appears only when the doc is in `review_mode: pending`. Proposed edits arrive here for you to accept or reject before they land. |
| **History** | Every pre-edit snapshot, click to restore. Also: **Undo**, **Reset** to baseline, **Save as baseline**. |
| **Skills** | Per-doc agent skills. Each `<section class="agent-skill">` in the doc body is read by the agent as authoritative for THIS doc; hidden from readers. Add / edit / delete inline. |
| **View ▾** | **Source** (raw markdown), **Graph** (cross-reference graph — math docs), **LaTeX** (export math doc as `.tex`). |
| **File ▾** | **Export** (single self-contained `.html`), **Print** (PDF or paper). |

## Themes

The **☀ / 🌙** toggle in the toolbar swaps light / dark. Choice persists per browser; first visit follows your OS preference. Live-renderer canvases (music notation, data grids, Mermaid diagrams) stay light in dark mode — they're authored against white and look wrong inverted.

## Document state

Every doc is a folder:

- `baseline.md` — the pristine starting point. **Reset** restores here.
- `current.md` — what you and the agent edit (gitignored).
- `snaps/snap-*.md` — automatic pre-edit history. **Undo** rolls back one; the **History** tab browses all.
- `assets/` — images, sounds, anything referenced by `![](assets/foo.png)`.

When you've reached a state you want to lock in, **Save as baseline** copies current → baseline. The old baseline is preserved as a snap, so it's never lost.

## Adding a new doc

Click **+ Doc**, or drop a file anywhere on the doc area:

- `.md` — loaded directly
- `.tex`, `.rst`, `.org`, `.txt` — server-side convert to AM
- `.pdf`, `.docx`, `.xlsx`, `.pptx` — server converts (vision for PDFs, structured for the rest)
- `.csv` — becomes a data figure (sortable / filterable)
- `.abc`, `.musicxml`, `.mid` — becomes a music figure (notation + playback)
- `.mmd`, `.mermaid` — becomes a diagram figure
- Other text-ish formats trigger a preview before sending to the agent

## Slash commands

- `/help` — full command list in the chat
- `/cancel` — interrupt the current agent turn
- `/undo`, `/reset`, `/save-baseline` — same as the History tab actions
- `/history`, `/skills`, `/source` — jump straight to that view
- `/model <name>` — switch model (starts a fresh chat)
- `/new` or `/clear` — fresh conversation

## Per-doc skills

Skills are the most powerful and most underused feature. Open the **Skills** tab and add one. Each skill is a small markdown chunk that gets injected into the agent's context for THIS doc only. Good uses:

- **Voice rules** — "Always write in first-person plural. Never use the word *comprehensive*."
- **Format conventions** — "Subtotals as `**Subtotal:** [N]`. Times in 0.1-hour increments."
- **Domain facts** — "Matter BJE000082 = Hernandez, Carlos. The warehouse client is *Acme Logistics*."
- **Templates** — "When I say 'log today', append a `## <date> <weekday>` section with subsections for each active client."

Readers don't see them in the rendered view (CSS hides `.agent-skill`); they ship with the doc and survive Export, Reset, and history restore.

## When something goes wrong

- **Edit didn't land?** The validator probably rejected it. Look in chat for the error — the agent already retried; after three failures it stops and tells you.
- **Doc looks corrupted?** **Reset** from the History tab (back to baseline) or **Undo** (back one snap).
- **Agent stuck or runaway?** `/cancel` ends the turn.
- **Can't click into a paragraph to edit?** A turn is in flight — wait for it to finish, or `/cancel`.
- **Math placeholders (`@@MATHI12@@`) visible in rendered text?** File an issue; that's a rendering bug.

## More

Source: <https://github.com/SemiSimpleMath/Adaptive-Markdown>

Want to learn by example? Try the `intro` doc (interactive math primer), `galois` (theorem-heavy math), `csv-test` (data figure), `beetangesample` (live music notation with transpose widget), or `mermaid-test` (diagram figures).
