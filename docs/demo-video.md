# Demo video: 1 minute 53 seconds

The requested segment estimates total two minutes. This plan trims seven seconds
to **1:53**, below 1:55 and leaving seven seconds under the challenge's 2:00 cap.
Beats only: dictate your own narration with Wispr Flow. Do not claim that editing
or captions alone prove how the project was built.

| Time | Screen | Narration beat | Prepare |
| --- | --- | --- | --- |
| 0:00–0:13 | YOUR REAL README line, then its failing command | Your own task; what you trusted; the concrete failure. **REAL INCIDENT SLOT: await your example.** | Use your own project, remove secrets; record its actual failure and corrected command. Never present Parcel as personal history. |
| 0:13–0:31 | Real pinned repo: terminal scan, carets and introduced-by commit | One stale statement; the precise stale token; the commit evidence | Pick one human-confirmed finding. Cache warm, terminal 120×32, font 18–22, color on. |
| 0:31–0:47 | Explain the same finding | Doc line → source evidence → commit; what is known and what needs review | Explain command pretyped; zoom only the relevant chain. |
| 0:47–1:01 | Fix preview and diff | Conservative suggested change; you review it | Preview only, no surprise write. Keep full diff in backup. Tool section totals 48 s. |
| 1:01–1:13 | Actual PR comment and supported suggestion | Two stale statements caught before merge; reviewer can inspect and accept a suggestion | Actual test PR/comment needs publication approval. Until then label the existing screenshot “local preview”. Do not invent a posted comment or one-click suggestion. |
| 1:13–1:24 | VS Code squiggle, edit, squiggle disappears | Feedback while writing; same evidence, fast correction | Install VSIX and lsp extra. Large font; diagnostics warmed. Editor section plus PR totals 23 s. |
| 1:24–1:38 | Public demo: paste URL, result, select finding, graph highlights | No sign-in; follow section → claim → symbol/file → commit | Browser 125%; pre-scan that exact pinned example. If response is cached, say so; do not imply an instant cold scan. |
| 1:38–1:48 | Benchmark results and merged upstream fix if one exists | Human-reviewed sample size and Wilson interval if available; scope limits; maintainer validation only if actually merged | Currently 12 scans, 511 blank review rows; **accuracy not measured**. 29/30 robustness scans complete, one timeout. No merged upstream proof yet. Show pending status if still true. |
| 1:48–1:53 | Actual Wispr Insights card, then 2 s of dictation | What you built hands-free; the role Flow played in your own words | Current usage card; real dictation clip; avoid displaying private prompts. |

## Choose an opening

- **Cold open:** your command fails before any title. Beat: “I followed my own
  README”; point to the stale token; then show the tool finding it.
- **Headline open:** the tagline on screen for two seconds, immediately followed
  by your real failure. Beat: the promise, personal consequence, evidence.

Both variants occupy the same first 13 seconds, not extra runtime.

Audience framing from you: beginners contributing to documentation, and teams
keeping docs aligned with the codebase. Use this as the benefit of the real
incident, not as a substitute for an incident that actually happened.

## Pre-recording checklist

- [ ] Fill the real incident slot and verify it yourself at the chosen commit.
- [ ] Select and human-check a real public finding; record URL, SHA, doc line,
      evidence, claim ID and expected patch. Do not use an unreviewed benchmark
      finding as a confirmed defect.
- [ ] Build a fresh synthetic backup with scripts/demo_setup.py. It creates Parcel,
      two fixed commits, warmed cache, HTML, JSON and ordered recording commands.
      Use a new output path for each reset; it never deletes an existing repo.
- [ ] Rehearse scan/explain/fix once; ensure scan output fits one screen.
- [ ] Pre-warm the exact real-repo cache and pre-scan the web example.
- [ ] Have the approved PR, editor, graph, benchmark and Wispr card tabs ready.
- [ ] Set browser zoom to 125%; VS Code font to at least 20; notifications off.
- [ ] Hide bookmarks, profile avatars, account names, home paths and recent files.
- [ ] Record backup clips before the final take. Keep the original recording.

## Terminal and recording

Use a clean PowerShell prompt (`function prompt { 'demo> ' }`) in a dedicated
terminal session. Use a dark high-contrast theme with distinct red/amber/green,
font 18 minimum (20–22 preferred), and 120 columns × 32 rows. Leave color on;
unset NO_COLOR and do not pass --no-color. At 1080p, crop to the terminal/editor
region rather than squeezing an entire 4K desktop into the frame. Verify a
sample upload on a laptop at normal playback size after compression.

For Windows OBS, set base/output canvas to 1920×1080, 60 fps, capture the app
window, and use a hardware H.264 encoder with a high-quality recording preset
(CQP around 18–22 is a starting point, then inspect text quality). Record MKV
so an interruption does not lose the take; remux to MP4. Use 48 kHz audio and
test microphone peaks before recording. These are starting settings, not a
promise that every GPU can sustain them. OBS's
[recording guide](https://obsproject.com/kb/standard-recording-output-guide)
recommends its high-quality recording presets and a resilient recording format.
For a simpler backup, use Windows Game Bar's Win+Alt+R in a supported app window;
test capture and audio first. It is not a general desktop recorder.

Cut every pause. If accelerating a wait, use 2–4× speed with a visible **2x** or
**4x** label. Do not replace a slow live result with an unlabeled cached result.
Add accurate captions and manually check flags, paths, SHAs and product names.
Dictate the caption draft in Flow if that is your actual process; do not imply
captions alone demonstrate Flow usage. Export an .srt/.vtt and upload it with the
video. Recheck the final encoded file's duration: target 113 s, mandatory <120 s.

## Risks and honest backup clips

| Risk | Backup |
| --- | --- |
| Real example is intentional or ambiguous | Use a different human-confirmed finding; otherwise show synthetic Parcel with an explicit label. |
| Scan is slow or attribution truncated | Cached recording from the same SHA, labeled pre-recorded/cached; retain the actual raw output. |
| Public demo is rate limited/offline | Pre-recorded production result with URL/date visible; do not claim a current successful live request. Fix hosting before submission. |
| PR comment/suggestion not posted | Clearly labeled local preview. It cannot stand in for the final actual-Action verification. |
| VS Code capture/diagnostics fail | User-recorded editor clip from a tested VSIX; a static screenshot is only an acknowledged fallback. |
| No human labels or merged upstream fix | Show “accuracy pending human review” and real scan/robustness counts. Omit merge claims. |
| Text illegible after compression | Crop closer, increase font, replay a test upload on laptop and phone. |
| Runtime goes over 1:55 | Remove transitions and pauses; keep all seven beats. Never accelerate narration until incomprehensible. |
| Private info appears | Stop and re-record after redaction; inspect captions and all frames before upload. |
