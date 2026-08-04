<!--
FINAL — Rivet Monitoring Dashboard v0.1 Question Architecture.
Transcribed from Rivet_Monitoring_Dashboard__v0_1__-_Question_Architecture.pdf

The original PDF has no Tag column. The `tag` column below is a proposed
architecture: one slug per strategic question, 1:1, so every classified
comment traces straight back to exactly which question it's evidence for.
These slugs are enforced as an enum in classify_comments.py's tool schema
(relevance_tag), so tagging can't drift across thousands of calls the way
free-text tagging would.

If you'd rather use different slugs, edit the `tag` column here AND update
RELEVANCE_TAGS in classify_comments.py to match — the two must stay in sync,
since the enum is hardcoded in the script for reliability rather than parsed
out of this file.
-->

# Question Architecture (final)

| ICP | Strategic Question | What we're listening for | Tag |
|---|---|---|---|
| All | What do they call this problem themselves? | Their own vocabulary for the gap/shortfall — not ours | `own-language` |
| All | What do they currently do about it, and how do they feel about that workaround? | References to existing tools/habits + sentiment toward them | `existing-workaround` |
| All | What objections or suspicion surface toward credit, fintech, or banks generally? | Skepticism, past bad experience, trust barriers | `trust-objection` |
| All | What specific moment makes the pain visible? | Payday, stipend date, restock day, rent due — the trigger, not just the feeling | `trigger-moment` |
| 1 | How fluently do they discuss the USDT/MEP mechanics? | Confident, technical language vs. anxious, uncertain language — signals how much "financial education" framing would land vs. condescend | `usdt-mep-fluency` |
| 1 | What's the sentiment toward employer/payroll timing? | Union-page language about pay dates, salary erosion — confirms employment profile and institutional trust level | `payroll-sentiment` |
| 2A | What does dependence on parents sound like? | Language around asking for transfers, timing anxiety, embarrassment or pride | `parent-dependence` |
| 2A | How does financial stress show up in humor? | Meme captions and comment sections on faculty humor accounts — comments often more revealing than posts | `stress-humor` |
| 2A | What small recurring costs create friction? | SUBE, photocopias, apuntes, transport — the specific line items, not just "no tengo plata" | `recurring-small-costs` |
| 2B | How do they talk about stipend adequacy against inflation? | Explicit stipend-vs-inflation math, "no alcanza" framing | `stipend-vs-inflation` |
| 3 | How do they describe their own ambition or plan? | "Get ahead" framing, savings goals, course enrollment as identity | `ambition-plan` |
| 3 | How do they describe wanting to leave gig work? | Side-project talk, transition plans, equipment/vehicle cost complaints | `leaving-gig-work` |
| 3 | How do they talk about investing in skills (English, coding)? | Framing of courses/learning as a financial strategy, not just self-improvement | `skill-investment` |
| 4 | How do they describe working-capital gaps? | Restocking, materials, fronting costs before client payment lands | `working-capital-gap` |
| 4 | What frustrations surface with AFIP/monotributo compliance? | Category confusion, tax friction, invoicing complaints | `afip-compliance` |
| 4 | How do they talk about client payment timing? | Late-paying clients vs. the seller's own need to pay upfront | `client-payment-timing` |

A comment that doesn't answer any of the above gets tag `none`.

## ICP definitions (for icp_likelihood)

- **1** — Crypto-capable salaried worker
- **2A** — Undergraduate student
- **2B** — Scholarship / CONICET fellow
- **3** — Gig worker / saving striver
- **4** — Monotributista small business owner
- **none** — Doesn't fit any ICP, or not enough signal to tell
