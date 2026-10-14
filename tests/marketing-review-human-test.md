Use **Marketing Content Reviewer** (`/marketing`) while signed in as a **CCO or nominated reviewer** with Product 2. For each case: fill Title / Type / Audience / Registrant, paste the copy below, submit, then expand **Details** and wait until status is `ready for cco` (not an approval).

The banner must stay: this is for CCO consideration, not an approval; figures are presentation-only.

---

### 1. Webpage (RIA homepage)

The screenshots for **Example 1 — Firm homepage (layout review)** remain the finding/layout source of truth. The **only content input** is the public homepage URL. Do not paste copy or attach a file.

| Field | Value |
|---|---|
| Type | Webpage |
| Audience | Retail |
| Registrant | RIA |
| Homepage URL | `https://…` (public https only) |

Reg AI fetches the page, scrapes visible content, fills the homepage template (hero, badges, testimonials, mid-page, footer), then analyzes it against:

- SEC 206(4)-1 (primary for RIA) and 204-2 (recordkeeping / saved artifact)
- FINRA 2210 + 2010 when the registrant is a broker-dealer or dual; 2211–2216 switch on by topic

`http://`, `localhost`, and private IPs must be rejected.

If you need a paste-only regression, the captured template still looks like:

```
Retire with confidence.
Granite Peak clients have earned an average of 11.4% annually since 2015.

Forbes Best-In-State Wealth Advisors 2025. Five Star Wealth Manager. A+ BBB Rating.

"Granite Peak doubled my portfolio. I finally sleep at night." — Janet R., client since 2018
"The only advisors I trust." — Mark T.

Our disciplined process has protected client capital in every downturn since the firm was founded.

Footer — 7pt grey, below the fold, after the sitemap: Past performance does not guarantee future results. The 11.4% annual figure is gross of fees. Testimonials are from current clients; no compensation was provided. Third-party rankings are not representative of all clients.
```

**Expect**
- Visual pass records hero, badge row, testimonial carousel, mid-page, buried footer
- Five keyed findings, top-to-bottom: hero gross-only 11.4%; badges held; “doubled my portfolio” testimonial; “every downturn” safety claim; buried footer
- **Do not** ask whether 11.4% is gross/net (footer already says gross of fees; net is missing → finding)
- **Do not** ask if the testimonial was paid (already “no compensation was provided”). If a page is silent on compensation, that question is **optional** — it can refine the disclosure, but it must not hold the finding or block CCO sign-off
- **Do** ask whether Granite Peak paid to be considered for or to display Forbes, Five Star, or BBB
- Answer: `Five Star involves a paid entry fee; Forbes and BBB do not.`
- After the answer: badge finding becomes add-disclosure (date/period on all three; paid-participation on Five Star only). Footer recommended option becomes **Covered by items 1–4**
- Suggested options: redline / add disclosure / pull — never Approve

---

### 2. Pitch deck performance slide

The screenshots for **Example 2 — Pitch deck performance slide** are the source of truth (Summit Advisory slide 7 of 14).

| Field | Value |
|---|---|
| Title | Summit Advisory pitch deck — slide 7 |
| Type | Presentation |
| Audience | Prospect |
| Registrant | RIA |

Paste (or attach a PDF/PPTX whose extracted text includes this). The review loops every slide: text embeddings, diagram embeddings, then a gap versus SEC 206(4)-1, SEC 204-2, FINRA 2210, and FINRA 2211–2216. The redline is one document covering the full deck.

```
Slide 7 of 14 — Summit Advisory pitch deck
Performance That Speaks for Itself

Bar chart — two bars, 2025 only:
Summit Growth Strategy: +22.4% (2025)
S&P 500: +18.1% (2025)

Gold callout: Backtested to 1999, our model would have returned 14.2% annually — through two crashes.

Bottom right 11pt: Returns shown before advisory fees. See appendix for disclosures.

Appendix, slide 14, 9pt: Standard performance disclosures, net figure for 2025 (+20.1%), no multi-period table.
```

**Expect**
- Tags: performance, hypothetical performance, benchmark comparison
- Five findings, in order: single-year cherry-pick (redline); gross on slide / net in 9pt appendix (redline); backtest callout (held); S&P comparison (add disclosure); “Performance That Speaks for Itself” (do nothing — puffery, flagged)
- **Do not** ask whether 22.4% is gross/net (slide already says before advisory fees)
- **Do** ask the screenshot pair: retail vs institutional vs both, **and** S&P total return vs price only
- Answer: `the deck goes to both audiences; the benchmark is total return.`
- After the answer: backtest cannot run in the retail version (two decks; methodology skeleton); benchmark needs one line that the S&P figure is total return
- Drafts: multi-period gross/net chart with [firm to supply] beyond 2025; drop gold callout from retail; footer 11pt with benchmark basis. Title may stay.
- Figures are presentation-only — not verified. Never Approve.

---

### 3. LinkedIn post (broker-dealer)

The screenshots for **Example 3 — LinkedIn post (advisor’s personal account)** are the source of truth (Daniel Ortiz / Cobalt Securities).

| Field | Value |
|---|---|
| Title | LinkedIn post — D. Ortiz personal account |
| Type | Social post |
| Audience | Retail |
| Registrant | **Broker-dealer** |

Paste:

```
Daniel Ortiz — Financial Advisor at Cobalt Securities
Markets are shaky but my clients aren’t worried 😎 Our structured note strategy pays 9% income while protecting your downside. DM me to learn how — spots are limited this quarter. #investing #passiveincome #wealthbuilding

214 reactions • 31 comments • 6 reposts
Jennifer Walsh: "Best move I ever made was switching to Daniel!"
Daniel Ortiz and 4 others like this.
```

**Expect**
- FINRA 2210 primary, 2010 underneath — not an RIA-only pass
- Visual pass records the post **and** visible engagement (the rep liked the client comment)
- Five keyed items: 9% + protecting downside (2210(d) redline/pull); principal approval (held); liked comment = adoption (unlike + do not engage); “spots are limited this quarter” (redline, **not** a question); 😎 is **not** a finding (do nothing — tone is fine)
- **Do not** ask gross/net, whether the commenter was paid, or whether scarcity is “real”
- **Do** ask: `Did this post go through Cobalt’s principal pre-approval process before it went up?`
- Answer: `no, it was posted directly`
- After the answer: **Pull** (take it down now — content finding **and** supervision gap). Unlike the comment. Route a corrected draft through pre-approval. Log the incident. Supervision follow-up is the CCO’s.
- Corrected draft drops 9%, scarcity, and absolute downside protection; adds partial buffers, issuer credit risk, and a structured-note suitability line. Never Approve.

---

### 4. Prospect email

The screenshots for **Example 4 — Prospect email (drip campaign, step 2 of 3)** are the source of truth (Meridian / Q3 Prospect Nurture). Merge fields stay untouched.

| Field | Value |
|---|---|
| Title | Q3 Prospect Nurture — step 2 of 3 |
| Type | Email |
| Audience | Prospect |
| Registrant | RIA |

Paste:

```
Template: Q3 Prospect Nurture — Step 2 of 3
Subject: {{FirstName}}, most advisors won't tell you this...
From: Sarah Kim <sarah.kim@meridianfs.com>
To: {{Email}}

Hi {{FirstName}},

Last quarter our retirement income model outperformed 87% of comparable strategies.
That's why WealthReview named us a Top 100 Advisor.
We only take on 12 new families per quarter, and August is filling fast.
Reply to this email and I'll personally run your free Retirement Readiness Score.

Sarah Kim, CFP
Senior Advisor, Meridian Financial Services

Footer: unsubscribe + firm address. No performance or rating disclosures.
```

**Expect**
- Templated list send = advertisement under 206(4)-1, not one-to-one. Tags: relative performance, third-party rating, scarcity
- Leave `{{FirstName}}` / `{{Email}}` alone
- Five findings: 87% relative claim (held); Top 100 Advisor — disclosures must travel with the rating into the email (add disclosure, **not** a question); 12 families / filling fast (held); subject-line concealment (optional redline); free Retirement Readiness Score (do nothing)
- Sequence flag: steps 1 and 3 not submitted; same findings if the template library reuses the 87% or the rating
- **Do not** ask gross/net or whether the firm paid for the rating
- **Do** ask: source of the 87% (who / period / universe); whether the 12-family cap is an enforced limit or copy
- Answers: `one-time internal Morningstar category comparison in April; unaudited` and `documented firm policy — 12-family cap is enforced`
- After: remove the 87% headline; keep the cap, strike “August is filling fast”; add WealthReview date/period/unpaid inline; optional subject → `{{FirstName}}, your Retirement Readiness Score is waiting`
- Output is a tracked-style draft, not an approval

---

### 5. Blog article (hybrid educational + promo)

The screenshots for **Example 5 — Blog article** are the source of truth (Meridian Insights, sequence-of-returns). Do not use the old duration / 18.2% paste.

| Field | Value |
|---|---|
| Title | Why Sequence-of-Returns Risk Is the Retirement Killer Nobody Talks About |
| Type | Blog / article |
| Audience | Retail |
| Registrant | RIA |

Paste:

```
Why Sequence-of-Returns Risk Is the Retirement Killer Nobody Talks About
By Sarah Kim, CFP® • July 14, 2026 • 6 min read
Insights > Retirement

Sequence-of-returns risk is why the order of market returns can matter more than the average return over 20 years. Two investors each start with $1 million, withdraw $50,000 a year, and average a 6% return.

Investor A years 1–3: -15%, -8%, -4%. Portfolio at year 20: $412,000.
Investor B years 1–3: +12%, +9%, +7%. Portfolio at year 20: $1,284,000.
The $872,000 gap is Investor A being forced to sell assets at a loss in the early years.

This is exactly why we built the Meridian Income Ladder. Our clients don't lose sleep over sequence risk, because our approach is designed to eliminate it.

In 2022 — the worst bond year in four decades — Meridian Income Ladder clients stayed on track while typical 60/40 retirees fell behind by double digits.

Want to see how the Income Ladder would work for your retirement? Book a free strategy call.
Button: Book my call
No disclosures anywhere on the page.
```

**Expect**
- Hybrid: paras 1–5 educational (do nothing, no performance tags); para 6 onward is the advertisement under 206(4)-1
- Tags attach to the promo half only: performance (implied), safety claim, strategy promotion
- Five keyed items: “designed to eliminate it” (redline — reword, not disclose); 2022 client/60/40 claim (held); paras 1–5 (do nothing); no disclosures on an ad-bearing page (add disclosure); free strategy call CTA (do nothing)
- **Do not** ask gross/net about the educational table percentages
- **Do** ask: whether “clients stayed on track” is all Income Ladder clients / a composite / selected accounts, and whether a documented basis exists for “double digits” vs 60/40
- Answer: `it’s a general recollection of how the strategy held up; no composite or documented comparison exists.`
- After: 2022 claim cannot be disclosed into compliance — rewrite as strategy-design commentary (or pull para 7). Paras 1–5 stay untouched. Footer disclosures added once the page is an ad.
- Output is tracked changes, not a webpage change-list. Never Approve.

---

### After each review

1. Confirm the header and performance disclaimer on the panel.
2. Answer only the questions shown; submitting answers should **change** held findings (not add trivia).
3. Pick an option per finding (redline, add disclosure, pull, do nothing, escalate).
4. Click **Send to CCO Review**. Status becomes `signed_off`. That records the selected options, writes a Gap Analysis, and promotes a RULE / GAP / FIX redline into the Review Queue. It does **not** mean the ad is approved or published.

If upload still fails with “Unable to persist,” the environment has not applied migration `20260817_0041`.
