# evil-read-arxiv Adaptation Notes

## Source Ideas To Reuse

- Skill-based workflow decomposition.
- `paper-analyze` style deep paper analysis sections:
  - abstract translation
  - research background and motivation
  - research question
  - method overview
  - experiments and results
  - contributions
  - limitations
  - related work positioning
- Markdown LaTeX rule:
  - inline formulas use `$...$`
  - block formulas use `$$...$$`
- `extract-paper-images` insight:
  - original paper figures are better than naive PDF image extraction
  - arXiv source extraction is useful for V2 image support

## Source Ideas Not To Reuse In V1

- arXiv ID as the primary input.
- Obsidian vault path and `OBSIDIAN_VAULT_PATH`.
- `20_Research/Papers` note layout.
- PaperGraph JSON.
- Daily recommendation workflow.
- Automatic arXiv source download as a required step.

## Zotero-First Replacement

- Primary input is Zotero title.
- Source of truth is Zotero MCP.
- Output is Zotero child note.
- Python CLI only performs deterministic local transformations.
- Codex performs interpretation, scientific judgment, and note writing.

## Better Notes Boundary

Better Notes is treated as a viewer and note-management enhancer. The generated child note should be readable in Better Notes, but this project does not call Better Notes internal APIs.

## V2 Figure Strategy

- Reuse from `extract-paper-images`: source-first acquisition order, safe source extraction, source/PDF provenance tags, and source-side figure PDF rendering.
- Reject from `extract-paper-images`: raw embedded-image extraction as the primary PDF strategy.
- New project choice: caption-anchored page crops remain necessary for local Zotero PDFs because many items are not resolvable to arXiv source.
- New execution lesson: opportunistic arXiv source plus deterministic PDF extraction plus OCR fallback is materially stronger than a PDF-only pipeline.
