<!-- Current evidence update: 22 October 2026 -->
Current build evidence: [real reproduced case](docs/REAL-CASE.md),
[supplied Flow cards](docs/built-with-voice.md), [AWS handoff](docs/AWS-HANDOFF.md),
[updated 1:50 shot list](docs/FLOW-RECORDING.md). The cards are included locally;
Flow-use clip, actual dictated prompts, independent human labels, PyPI publication,
public deployment and event upload remain incomplete. Earlier schedule/checklist
entries below are plans, not completed evidence.

# No Hands challenge submission pack

Deadline supplied by the owner: **Friday 30 October 2026, 23:59 IST**
(18:29 UTC). Target: submit **Thursday 29 October, 19:00 IST**
(13:30 UTC), retaining a full day of margin. No submission has been made.

## Owner checklist

- [ ] Working public demo URL: **DEFERRED by owner for AWS deployment**. It opens without
      sign-in. Link the actual GitHub repository and PyPI project from the demo.
- [ ] On a physical laptop, open a fresh private/incognito window; load the demo,
      open one example, scan one live public repo, select a finding and its graph.
      Record device/browser/date, URL and scan ID.
- [ ] Repeat on a physical phone in a private browser. Browser viewport emulation
      is useful QA but does not satisfy this physical-device check.
- [ ] Demo video: **PENDING owner recording/upload**. YouTube unlisted/public;
      captions uploaded and checked; final encoded duration <2:00, target 1:53.
      [Shot list and rehearsal checklist](docs/demo-video.md).
- [ ] Wispr Flow desktop app → Insights → usage card screenshot uploaded.
      Upload **weekly during the challenge**, not only at submission.
- [ ] Final short description dictated by you in Flow, in your own voice.
- [ ] PyPI install, Action/PR, production smoke, full-history secret scan,
      real links and uptime verification table all reviewed.
- [ ] Main/default branch contains the exact submitted state.
- [ ] After approval, tag its commit submission-2026-10-30 and publish that tag.
- [ ] Preserve submitted commit SHA, production image digest, video URL/duration,
      captions, card screenshots, submitted text and confirmation receipt.

## Schedule in IST

| Date | Finish |
| --- | --- |
| 18–20 Oct | Trusted-publisher setup by owner; review release candidate, personal incident and demo state. |
| 21–23 Oct | Approve TestPyPI release; pass install matrix; fix any package omissions; human-label benchmark samples. |
| 24–25 Oct | Approve stable PyPI/GHCR/GitHub Release and hosting; test a real PR and production scans. Upload this week's usage card. |
| 26–27 Oct | Record/re-record 1:53 video; dictate description and captions; verify editor and graph shots. |
| 28 Oct | Private phone/laptop checks, final table, video duration and links; activate/check monitor. |
| 29 Oct by 19:00 | Upload current usage card, approve submission commit/tag, submit and archive receipt. |
| 30 Oct | Buffer for actual submission problems; 23:59 deadline. Begin deployment freeze. |
| 31 Oct–5 Nov | Keep demo running; monitor alerts; work on a branch. Raise score questions by 5 Nov. |
| 6 Nov | Winners announced; keep demo and monitor running through the end of the day IST. |

## Weekly Wispr card reminders

The challenge start date has not been supplied. Earlier weekly obligations cannot
be reconstructed honestly. Tell us the real start date; run
scripts/submission_reminders.py with --start YYYY-MM-DD to generate one upload
date per seven-day challenge week through 30 October, including a final partial
week. Do not backdate a screenshot or tick a past upload without proof.

Upcoming reminders while the start date is pending:

- [ ] 18 Oct: save/upload the current Insights card; reconcile earlier weeks.
- [ ] 25 Oct: upload a new weekly card and save the receipt.
- [ ] 29 Oct: upload the final current card before the planned submission.
- [ ] 30 Oct: check whether the actual start-date schedule requires a final-day
      upload in addition to the 29 Oct submission card.

## Short description: dictate three sections

### 1. The problem and who it affects

- Personal incident slot: your actual project, stale doc line, failed command,
  cost to your work and corrected behavior. Still awaiting your example.
- Your audience framing: help beginners contribute to documentation and help
  maintainers keep docs updated with the codebase. Evidence and conservative
  patches give a new contributor a concrete place to start; human review matters.
- Tagline: find unsupported doc lines and the commit that broke each one.

### 2. What I built and the evidence

- Static source inspection, evidence/commit attribution, scan/explain/fix preview,
  CI reports, offline HTML graph, optional language server/editor and web demo.
- Honest partial framework coverage; no target code execution; model calls opt-in.
- Frozen benchmark: 12 pinned repos, 511 blank human-review rows (151 precision,
  360 recall audit). Accuracy/intervals remain unknown until you label them.
- Robustness: 29 completed, one timeout, zero observed scan crashes. Speed
  targets currently fail; large scoped run peak traced Python allocations
  133,127,735 bytes. Link [measured results](docs/RESULTS.md), not fixture accuracy.
- Source: https://github.com/ayushagarwalhere/docs-drift. Demo, video, PyPI release
  and merged upstream proof remain pending; insert only verified live links.

### 3. How I used Wispr Flow

- Your own facts: what you dictated, how you iterated, what Flow changed about
  building hands-free. Fill with your actual process and Insights card evidence.
- Description and narration will be dictated by you; these bullets are a scaffold,
  not a finished personal claim. Include weekly usage-card links/receipts.

## Progress post ideas, not finished posts

| Idea | One-line angle | Visual |
| --- | --- | --- |
| First public-repo drift | A human-confirmed stale instruction at an exact commit; disclose the tool and review. | Doc/code comparison with pinned SHA; no unreviewed accusation. |
| Graph | Follow one statement from docs through code to the introducing change. | Highlighted actual evidence path. |
| PR comment | Catch documentation breakage while reviewing the code change. | Actual approved test PR comment/suggestion; otherwise label preview. |
| Honest benchmark | What worked, what timed out, and what still needs human labels. | Real table with sample sizes/intervals or pending labels and failures. |
| Launch | The first install and sign-in-free demo are ready to try. | Actual successful pipx scan plus deployed URL, after publication. |

Use #NoHandsChallenge and tag the verified WeMakeDevs/Wispr Flow accounts on
your chosen platform. Verify the actual handles before posting; no posts have
been sent. Dictate the final text yourself.
