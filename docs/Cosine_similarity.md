Cosine similarity is the app’s **relatedness score between two pieces of text that have been turned into embeddings**. It does not decide compliance by itself. It decides **which documents and rule excerpts are close enough to feed the LLM**, and it is used as a **quality gate** on generated redlines.

The same math is reused in marketing review, Internal Policy Gaps, business-change matching, gap-analysis retrieval, and redline validation.

---

## The shared metric

Text is split into overlapping chunks (about 3,600 characters, 400-character overlap), then embedded with OpenAI **`text-embedding-3-large`** (3,072 dimensions). Those vectors live in Postgres **pgvector**.

**Pairwise cosine** between two vectors is the usual formula:

\[
\cos(\mathbf{a},\mathbf{b}) = \frac{\mathbf{a}\cdot\mathbf{b}}{\|\mathbf{a}\|\,\|\mathbf{b}\|}
\]

Scores are clipped to \([-1, 1]\). Near **1** means the chunks point the same way (same topics, similar wording). Near **0** means little overlap. OpenAI embeddings are normalized, so this is essentially how aligned two passages are in meaning-space.

Documents are many chunks, not one vector. The product score is **mean-max cosine**:

1. For each chunk on the **query** side, take the **best** cosine against any chunk on the **reference** side.
2. Average those best-match scores.

So a business change that only overlaps one section of a long policy can still score well, instead of being diluted by unrelated chapters.

Postgres nearest-neighbor search uses **cosine distance**. The API converts that back to similarity with `1 - distance`.

---

## 1. Marketing review: which SEC/FINRA rules apply

When a homepage, email, blog, social post, or pitch deck is reviewed, the pipeline:

1. Builds one embeddable blob from body text, layout notes, and (for decks) per-slide text/diagrams.
2. Embeds that content.
3. Embeds official marketing-rule documents (live eCFR/FINRA corpus when possible; emails and social posts use a faster **fallback corpus** so they are not blocked on downloads).
4. Scores **content vs each rule document** with mean-max cosine.
5. Marks a rule **related** if the score is at least **0.25**.
6. Ranks individual **rule chunks** the same way and keeps the top **8** excerpts above that floor.

Those excerpts are the only official rule text the review LLM is told to gap-analyze against. The prompt is explicit: compare the ad to **those cosine-selected excerpts**, not the whole Marketing Rule. FINRA topic rules 2211–2216 are applied only if they showed up in that related set.

For **pitch decks**, the same comparison is also run **per slide** (slide text + diagram captions vs each rule), then rolled into one deck-wide gap.

What you see:

- Non-deck reviews show a line like `Cosine-related rules: 206(4)-1 (0.41) · 204-2 (0.22)`.
- Deck reviews label related rules on the deliverable (topic rules need **0.50** before they are shown).
- The same cosine payload is stored on the review and used when a Marketing Gap is written (rule codes, citations).

So cosine here is a **retrieval filter**: it picks which advertising rules are “about this piece,” then the LLM writes findings against that subset.

---

## 2. Internal policies vs Latest Regulations

When an Internal Policy is uploaded or re-evaluated, the workflow is **embed → cosine → gap analysis**.

After the policy chunks are stored, the app computes a **baseline mean-max cosine** between that policy and each Latest Regulations document (FINRA, SEC, CFTC). That score is stored as `regulator_document_similarity` on the gap row.

On Internal Policy Gaps, **Added content** is shown as:

`100% − (cosine × 100)`

A **high** cosine means the policy already looks a lot like the regulations (less “new” relative to the corpus). A **low** cosine means more of the policy sits away from that regulatory language, so the UI treats it as more added/divergent content. That is a **coverage/overlap heuristic**, not a finding count.

The **RULE / GAP / FIX** sentences still come from the LLM. Cosine only:

- records how close the whole policy is to each regulator corpus, and
- helps retrieval (next section) choose which regulation passages to put in the prompt.

---

## 3. Business changes: which Internal Policy to revise

A business change is chunked and embedded, then scored against every active Internal Policy with mean-max cosine.

- If the **best** policy scores **≥ 0.25**, that policy is the match (`mode: COSINE`). No LLM match step.
- If every score is below 0.25, an LLM (then a lexical fallback) tries to match. If those fail, the **closest cosine policy** can still be used so the workflow does not stall.

The winning score is stored as `business_change_policy_cosine_similarity` and shown as **Added content** on a Business Change gap row (same `100% − cosine%` display). Here a **high** score means “this change looks like that policy”; a **low** score means the change is farther from existing policy language.

---

## 4. Gap-analysis retrieval (nearest neighbors)

Sentence-level gap analysis does not embed the whole policy as one vector against the whole CFR. It **searches**:

1. Embed a query derived from the policy/change.
2. Ask pgvector for the closest **Internal Policy** chunks (`PolicyChunkPgVectorStore`, ordered by cosine distance).
3. Ask pgvector for the closest **government regulation** chunks (`policy_comparison_service`).

Those neighbors become the excerpts in the LLM prompt (`government_reference_excerpts`, chunk scores in `retrieval_metadata`). Cosine is how the system answers: **“which passages of FINRA/SEC/CFTC are nearest this policy language?”** The model then writes RULE / GAP / FIX against those passages.

---

## 5. Redlines: accept/retry gate and Insights

After a tracked redline is generated, its text is embedded and compared with **pooled FINRA + SEC + CFTC** regulation embeddings (mean-max again).

- Default threshold: **`REDLINE_MIN_VECTOR_SIMILARITY = 0.25`**.
- Below that, the draft is **rejected** and drafting can retry (up to 4 times).
- The same family of scores is stored on the revision (`redline_internal_cosine_similarity`, `redline_regulator_cosine_similarity`).

The CCO Review Queue can show a **content-added** percentage from the regulator score (`(1 − cosine) × 100`). Insights has a histogram of **redline vs Internal Policy** mean-max cosine: higher means the proposed language stayed closer to the existing policy.

If embeddings cannot be produced (missing API key, local/dev), validation is **skipped** so review is not blocked; production is stricter.

---

## How to read a score in this product

| Score | Typical meaning here |
| --- | --- |
| **High** (e.g. 0.7+) | Chunks are talking about similar things. For matching, “this is the right document.” For redlines, “this draft still sits in the regulatory/policy neighborhood.” |
| **Around the floor** (**0.25** for marketing, business change, gap retrieval, and redline) | “Related enough to use.” Below the floor, that rule/policy/draft is treated as unrelated or insufficient. |
| **Low** | Different subject matter — or a large rewrite relative to the reference corpus. |

Cosine never approves an ad, never files a gap, and never signs off a redline. It **narrows the search space** and **checks that generated text is still in the right neighborhood**. The LLM (and then the CCO) still make the compliance call.