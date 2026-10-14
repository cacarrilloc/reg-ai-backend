# Phase 6 - Redline Generation

Phase 6 adds a human-review redline generation path on top of persisted Phase 5 gap analyses.

- `POST /api/v1/reviews/gap-analyses/{gap_analysis_id}/redline` generates or returns the pending policy revision for a gap analysis.

- The service loads the source policy DOCX from Supabase Storage and uses Claude to draft sentence-level replacements (`anchor_text` + `replacement_text`) grounded in the gap findings.

- Replacements are applied directly to the **original internal policy DOCX** using `docx-revisions`, enabling Word track-changes mode (`w:trackRevisions`) and writing true OOXML tracked deletions and insertions with `Reg AI` as the revision author.

- The generated tracked redline itself is embedded and compared with the pooled FINRA, SEC, and CFTC regulation embeddings. Drafting retries when cosine similarity is below the configured threshold.

- The generated tracked-change DOCX is uploaded to the `policy-redlines` bucket and a `PolicyRevision` row is created with `status='PENDING'`. Review Queue downloads serve this tracked internal-policy file.

The generated redline remains decision support. It does not update the canonical policy document until a later CCO review workflow approves it.

Local verification covers DOCX structural compatibility: the output opens with `python-docx`, contains `w:trackRevisions`, `w:del`, `w:ins`, and complete comments wiring. Full visual render QA requires LibreOffice and the document renderer dependencies on the host.
