from __future__ import annotations

import paper_reader.evidence_manifest as evidence_manifest_module
from paper_reader.evidence_manifest import (
    EvidenceFigureMember,
    EvidenceManifest,
    EvidenceResourceCheck,
    EvidenceSectionMember,
    EvidenceTableCandidateMember,
)


def locator_membership_error(locator: str, manifest: EvidenceManifest) -> str | None:
    assert hasattr(evidence_manifest_module, "locator_membership_error")
    return evidence_manifest_module.locator_membership_error(locator, manifest)


def _manifest() -> EvidenceManifest:
    return EvidenceManifest(
        format="paper_reader.evidence.v2-internal",
        evidence_id="evidence_test",
        run_id="run_test",
        created_at="2026-07-10T09:30:00Z",
        source_sha256="a" * 64,
        complete=True,
        degraded=False,
        preview_pages=None,
        files=(),
        pages=(1, 2, 3),
        sections=(EvidenceSectionMember(title="Methods", start_page=2, end_page=3),),
        table_candidates=(EvidenceTableCandidateMember(index=1, page=2, section="Methods"),),
        figures=(
            EvidenceFigureMember(
                figure_id="fig_p2_1",
                page=2,
                artifact_path="evidence/evidence_test/figures/figure.png",
            ),
        ),
        resource_checks=(
            EvidenceResourceCheck(
                name="pdf_page_count",
                status="passed",
                actual=3,
                limit=500,
            ),
        ),
    )


def test_locator_membership_accepts_only_exact_manifest_members() -> None:
    manifest = _manifest()

    assert locator_membership_error("context.md page 1", manifest) is None
    assert locator_membership_error("context.md page 2 section Methods", manifest) is None
    assert (
        locator_membership_error(
            "context.md page 2 section Methods table_candidate 1",
            manifest,
        )
        is None
    )
    assert locator_membership_error("figure_context.md fig_p2_1", manifest) is None


def test_locator_membership_rejects_nonmembers_and_noncanonical_sources() -> None:
    manifest = _manifest()

    assert locator_membership_error("context.md page 4", manifest) == "page_not_in_evidence"
    assert (
        locator_membership_error("context.md page 2 section Results", manifest)
        == "section_not_in_evidence"
    )
    assert (
        locator_membership_error(
            "context.md page 2 section Methods table_candidate 2",
            manifest,
        )
        == "table_candidate_not_in_evidence"
    )
    assert (
        locator_membership_error("figure_context.md fig_p2_missing", manifest)
        == "figure_not_in_evidence"
    )
    for locator in (
        "context.md",
        "figure_context.md",
        "page 2 method section",
        "section_context.md page 2 section Methods",
        "secondary_contexts/source.md",
    ):
        assert locator_membership_error(locator, manifest) == "noncanonical_locator"
