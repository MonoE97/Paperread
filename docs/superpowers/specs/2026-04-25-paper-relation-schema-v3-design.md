# Paper Relation Schema V3 Design

**Status:** Deferred.

This spec is preserved as the design record for a future paper-relation layer, but it is not the active implementation target. Review found that V3 still needs two product decisions before it is safe to implement:

- where related-paper candidates must come from
- where cross-run relation deduplication state should live

Current development has shifted back to V2 hardening: render a small set of paper labels at the end of each note and prevent accidental duplicate analysis when a Zotero item already has a Codex summary note.

## Summary

V3 extends the existing Zotero-first paper summary workflow with a paper-centric relation layer. The goal is not to build a full graph product yet. The goal is to let each analyzed paper emit a small, controlled set of normalized labels plus a few high-value related-paper links at the end of the generated note, while keeping the internal schema strict enough to support later graph integration.

This design keeps the current project boundaries intact:

- Zotero access stays behind `zotero-mcp`
- note generation stays inside the existing Python CLI and Jinja template flow
- Better Notes remains an optional display layer and is not a runtime dependency for V3

V3 therefore adds structured relation inference to the existing summary pipeline, but only exposes a minimal rendering in the final note:

- `## 本文标签`
- `## 相关文章`

Everything else needed for deduplication, relation quality control, and later graph sync remains internal.

---

## Why V3 Exists

The current workflow is strong at single-paper understanding:

- it finds the Zotero item
- extracts text and key figures
- generates a figure-aware structured note
- writes a Zotero child note when explicitly requested

What it does not yet do is preserve cross-paper structure. After enough analyzed papers accumulate, the user needs lightweight answers to questions like:

- Which papers are method-neighbors of this paper?
- Which papers are about the same problem but in a different materials domain?
- Which papers should I jump to next from this note?

The wrong solution would be to jump directly to a Better Notes graph integration. That would make the system more coupled before the data model is stable. V3 instead defines the relation schema first, keeps the rendered note lightweight, and postpones any plugin-level graph bridge until the relation quality is proven in real usage.

---

## Goals

1. Add a normalized paper-level label model to the summary workflow.
2. Add a paper-level relation model that can represent candidate and accepted relations.
3. Allow the system to publish a very small number of accepted relations automatically.
4. Render only labels and lightweight related-paper summaries in the final note.
5. Prevent graph pollution from note versioning, weak keyword overlap, and duplicated relations.

## Non-Goals

1. No Better Notes API integration in V3.
2. No graph visualization in V3.
3. No batch full-library relation rebuild in V3.
4. No note-to-note graph in V3.
5. No automatic relation syncing back into Zotero plugin state in V3.
6. No user-facing display of `paper_uid`, relation confidence, or internal relation metadata in the note.

---

## Scope Boundary

V3 covers only the relation schema and its direct use in the current note-generation workflow.

It does include:

- normalized labels
- related paper discovery output structure
- candidate-vs-accepted relation policy
- note rendering rules for labels and related papers
- deduplication rules
- pollution-control rules

It does not include:

- new Zotero write primitives
- plugin-side graph storage
- Better Notes relation APIs
- external databases

---

## Design Principles

### 1. The graph entity is the paper, not the note

The current workflow intentionally produces versioned notes:

`[Codex Summary] <paper title> - YYYY-MM-DD`

If those notes become graph nodes, the graph will fill with duplicates for the same paper. V3 therefore defines the paper as the only primary graph entity. Notes are derived artifacts.

### 2. Internal schema can be rich even when note rendering is minimal

The final note should stay readable. The internal model still needs stronger structure for:

- deduplication
- accepted-relation gating
- future graph export

So V3 separates:

- internal relation records
- rendered relation summaries

### 3. Automatic publication must be conservative

V3 allows automatic accepted relations, but only in a tightly bounded way:

- accepted relations are limited in count
- accepted relations must be typed
- accepted relations must carry a reason
- weak overlap stays at candidate level

### 4. Labels must be normalized keys

Rendered labels use English normalized keys, not free-form prose and not Chinese display labels.

Examples:

- `metasurface`
- `inverse_design`
- `deep_learning`
- `power_allocation`

This keeps future matching and deduplication tractable.

---

## Alternatives Considered

## Option A: Labels Only

Only generate normalized labels and do not infer any paper-to-paper relations.

### Advantages

- easiest to implement
- almost no graph pollution risk
- minimal schema complexity

### Disadvantages

- does not answer "what should I read next?"
- loses obvious method and problem neighbors

## Option B: Paper-Centric Relations With Candidate and Accepted Layers

Generate paper labels, candidate relations, and a small number of accepted relations. Render only lightweight labels and related-paper titles plus reasons in the note.

### Advantages

- matches the current workflow structure
- provides immediate reading value
- controls pollution with gating and limits
- keeps later graph integration possible

### Disadvantages

- more schema design work than labels alone
- requires stronger normalization and deduplication rules

## Option C: Direct Better Notes Graph Integration

Generate note links or plugin-native relations directly in Better Notes.

### Advantages

- immediate graph-like experience

### Disadvantages

- violates the current project boundary
- hard to test
- tightly couples data quality to plugin runtime behavior
- highest pollution risk

## Recommendation

Choose Option B.

It gives real value in the current note flow without forcing plugin integration too early.

---

## Core Data Model

## 1. Paper Identity

Each analyzed work is represented internally as one stable paper entity.

### `paper_uid` generation priority

1. `doi:<normalized-doi>`
2. `arxiv:<normalized-arxiv-id>`
3. `zotero:<item-key>`

This prevents the note-version explosion from turning into a graph explosion and gives a deterministic identity even when only partial metadata exists.

### Example

```json
{
  "paper_uid": "doi:10.1038/example",
  "zotero_item_key": "8HCYMEEB",
  "title": "Deep-Learning Assisted Polarization Holograms",
  "paper_type": "research_article",
  "year": 2024,
  "doi": "10.1038/example",
  "arxiv_id": null
}
```

## 2. Note Version

Notes remain versioned write artifacts and are not primary graph entities.

### Example

```json
{
  "note_uid": "zotero-note:V25DPIFT",
  "paper_uid": "doi:10.1038/example",
  "zotero_note_key": "V25DPIFT",
  "generated_at": "2026-04-25",
  "summary_version": "v3",
  "status": "written"
}
```

V3 stores note-version metadata only as provenance. It does not use note versions for relation matching.

## 3. Paper Labels

Labels are normalized English keys grouped by semantic role.

### Label groups

- `topics`
- `method_family`
- `material_system`
- `task_type`
- `evidence_type`

### Example

```json
{
  "paper_profile": {
    "paper_type": "research_article",
    "topics": ["polarization_holography", "metasurface_inverse_design"],
    "method_family": ["deep_learning", "inverse_design"],
    "material_system": ["programmable_metasurface"],
    "task_type": ["hologram_generation", "power_allocation"],
    "evidence_type": ["simulation", "experiment"]
  }
}
```

These grouped labels are used internally. The rendered note flattens them into one list of normalized keys.

## 4. Relation Candidate

Relation candidates are automatically inferred links that have not crossed the accepted-relation publication bar.

### Example

```json
{
  "source_paper_uid": "doi:10.1038/example",
  "target_paper_uid": "arxiv:2501.08998",
  "target_title": "CrystalGRW: Generative Modeling of Crystal Structures with Targeted Properties via Geodesic Random Walks",
  "relation_type": "same_method_family",
  "direction": "undirected",
  "confidence": 0.78,
  "status": "candidate",
  "reason": "Both use learning-driven structure design with target-aware optimization.",
  "shared_labels": {
    "method_family": ["deep_learning", "inverse_design"]
  }
}
```

## 5. Accepted Relation

Accepted relations are a stricter subset of relation candidates and are eligible for note rendering.

### Example

```json
{
  "source_paper_uid": "doi:10.1038/example",
  "target_paper_uid": "arxiv:2501.08998",
  "target_title": "CrystalGRW: Generative Modeling of Crystal Structures with Targeted Properties via Geodesic Random Walks",
  "relation_type": "same_method_family",
  "direction": "undirected",
  "confidence": 0.86,
  "status": "accepted",
  "reason": "Both follow target-driven learned structure design, although one focuses on metasurfaces and the other on crystals.",
  "shared_labels": {
    "method_family": ["deep_learning", "inverse_design"]
  }
}
```

Accepted relations remain internal structured objects. The note renders only title plus a shortened reason.

---

## Relation Types

V3 intentionally limits accepted relation types to six.

## Undirected

- `same_problem`
- `same_method_family`
- `same_material_system`

## Directed

- `compares_against`
- `extends`
- `survey_of`

These six are enough to make the note useful without encouraging vague graph growth.

### Not allowed as accepted relation types in V3

- `similar_to`
- `adjacent_to`
- `possibly_related`
- generic topic overlap

Those may exist as weak internal reasoning signals, but they do not publish as accepted relations.

---

## Acceptance Policy

V3 allows the system to publish a few accepted relations automatically, but only under strong rules.

## Accepted relation gate

A relation may be published into `relations` only if:

1. it matches one of the six allowed relation types, and
2. it includes a non-empty reason, and
3. it passes one of these thresholds:
   - a strong single relation signal, or
   - at least two medium-strength shared dimensions with a specific reason

If these conditions are not met, the relation remains in `relation_candidates`.

## Publication limits

To prevent note pollution:

- accepted relations per paper: maximum `4`
- accepted relations per relation type: maximum `2`

If more relations qualify, keep the top-scoring ones internally and render only the highest-value subset.

---

## Deduplication Rules

## 1. Paper Deduplication

Paper identity uses the `paper_uid` priority described above.

If a paper later gains a better identity source, merge to the higher-priority identity:

- Zotero fallback identity can upgrade to arXiv identity
- arXiv identity can upgrade to DOI identity

The merge rule is always one-way toward the highest-priority stable identifier.

## 2. Note Deduplication

Note versions are never used for primary deduplication.

The same paper can have multiple note versions. They all map back to one `paper_uid`.

## 3. Relation Deduplication

### Directed relations

Unique key:

```text
(source_paper_uid, target_paper_uid, relation_type, direction)
```

### Undirected relations

Normalize ordering first:

```text
(min(uid_a, uid_b), max(uid_a, uid_b), relation_type)
```

### On duplicate match

Do not create a second edge. Update the existing relation with:

- latest `confidence`
- latest `reason`
- merged `shared_labels`
- `last_seen_at`

This keeps relation history stable while still letting repeated analyses strengthen or refine the edge.

---

## Pollution-Control Rules

## 1. Weak labels cannot publish a relation by themselves

Shared generic labels like:

- `deep_learning`
- `ai4s`
- `materials_science`

are not sufficient on their own for an accepted relation.

## 2. Accepted relations must stay sparse

The hard limits are:

- total accepted relations rendered in note: `<= 4`
- accepted relations per type: `<= 2`

## 3. Free-text labels are never treated as normalized labels directly

The system may infer free-text concepts during analysis, but only normalized keys can enter published labels.

Anything that is not normalized must remain internal as a proposed label candidate.

## 4. Notes never form automatic graph edges

Only papers relate to papers. Notes do not relate to notes automatically in V3.

## 5. Survey papers follow different relation expectations

For surveys and perspectives:

- `survey_of` can be accepted more easily
- `extends` and `compares_against` should be applied more carefully

This avoids forcing primary-research relation logic onto review-style papers.

---

## Rendered Note Contract

The final note remains intentionally minimal.

## 1. Required new sections

At the end of the note, V3 adds:

- `## 本文标签`
- `## 相关文章`

## 2. `本文标签` rendering

This section renders a flattened ordered list of normalized keys.

### Rendering order

1. fixed system labels
   - `codex-summary`
   - `paper-summary`
2. normalized inferred labels from the paper profile

### Example

```md
## 本文标签

- codex-summary
- paper-summary
- metasurface
- inverse_design
- deep_learning
- power_allocation
```

No Chinese aliases, no confidence values, and no label categories are shown in the note.

## 3. `相关文章` rendering

This section renders accepted relations only.

Each entry contains:

- target paper title
- one short reason sentence

### Example

```md
## 相关文章

- Deep-Learning Assisted Polarization Holograms  
  Both study deep-learning-based inverse design for metasurfaces, but this paper focuses more on power allocation control.

- CrystalGRW: Generative Modeling of Crystal Structures with Targeted Properties via Geodesic Random Walks  
  Both follow target-driven structure design, while the application domain shifts from metasurfaces to crystal structures.
```

The note does not render:

- `paper_uid`
- `relation_type`
- `direction`
- `confidence`
- `shared_labels`
- candidate-only relations

---

## Integration With Current Workflow

V3 is designed to fit into the existing pipeline without changing the current Zotero boundary.

## Current pipeline reference

Today the workflow already produces:

- `metadata.json`
- `extract.json`
- `context.md`
- `figures.json`
- `figure_context.md`
- `summary.json`
- `note.md`

V3 extends the summary and rendering stages.

## Summary contract additions

The generated `summary.json` should be extended with:

```json
{
  "paper_profile": {
    "paper_type": "",
    "topics": [],
    "method_family": [],
    "material_system": [],
    "task_type": [],
    "evidence_type": []
  },
  "relation_candidates": [],
  "relations": []
}
```

## Rendering contract additions

The note renderer should derive:

- `note_render_labels`
- `note_render_relations`

from the richer internal summary structure.

### `note_render_labels`

A flattened ordered list of normalized keys.

### `note_render_relations`

A minimal list of:

- `title`
- `reason`

This lets the template remain simple even though the internal schema is richer.

---

## File-Level Impact

V3 should affect the existing project in these places:

- `src/zotero_paperread/note.py`
  - extend rendering context validation for new sections
- `templates/zotero_note.md.j2`
  - add `本文标签` and `相关文章` at the end of the note
- summary-generation logic used by the skill
  - add `paper_profile`, `relation_candidates`, and `relations`
- tests covering note rendering and summary contract validation

V3 should not require changes to:

- Zotero write primitives
- Better Notes runtime
- PyMuPDF extraction stack

---

## Risks and Mitigations

## Risk 1: relation quality is too weak

If the relation inference logic is too permissive, accepted relations become noisy.

### Mitigation

- accepted relation types are tightly limited
- accepted relation count is capped
- weak overlaps remain candidates only

## Risk 2: label sprawl

If labels are not normalized, near-duplicates like `deep_learning`, `deeplearning`, and `neural_networks` will fragment the graph.

### Mitigation

- normalized keys only
- free text stays out of rendered labels

## Risk 3: note-version pollution

If note versions are treated like graph entities, the graph will duplicate the same paper repeatedly.

### Mitigation

- paper is the only primary entity
- note version is provenance only

## Risk 4: overexposing internal mechanics in the note

If confidence scores and relation metadata are rendered, the note becomes implementation-heavy and harder to read.

### Mitigation

- note renders only label keys, titles, and one-sentence reasons

---

## Acceptance Criteria

The V3 design is considered successful if:

1. each analyzed paper can emit a normalized `paper_profile`
2. the system can produce both `relation_candidates` and a bounded set of accepted `relations`
3. accepted relations are deduplicated at paper level, not note level
4. the rendered note only shows:
   - normalized labels
   - related paper titles
   - one-sentence reasons
5. the design remains fully compatible with the existing Zotero-first workflow and does not require Better Notes integration

---

## Deferred Work

These are intentionally left for later versions:

- Better Notes relation sync
- graph visualization
- user-confirmation workflow for promoting candidates to accepted relations
- collection-wide relation rebuilding
- external graph storage

V3 is the schema and rendering foundation, not the full graph product.
