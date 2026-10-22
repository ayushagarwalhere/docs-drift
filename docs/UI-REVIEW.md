# Monochrome judge experience

Implemented 22 October 2026. Inspiration reviewed from
[Company Brain](https://github.com/ayushagarwalhere/company_brain) at
`638a982ecd03204377e0115a5344d4c051a10e66`: large typography, fine borders,
numbered sections and perspective. The docs-drift layout and interactions are
implemented in its existing templates, CSS and JavaScript, without adding a
frontend runtime or external font dependency.

## What changed

- Black-and-white themes across the landing page, supporting pages and reports.
- Three perspective layers illustrate the actual rich-cli claim, declaration
  and manually inspected commit. Native scrolling drives subtle depth changes;
  a motion toggle and OS reduced-motion preference disable animation.
- A prominent scanner accepts a full GitHub URL or `owner/repository` shorthand.
  Duplicate submits are ignored; validation and request timeout errors are shown
  inline. No API key or sign-in is required for these rule-based public scans.
- Real saved reports are immediately available. Synthetic fixtures remain in a
  separately labeled disclosure. Review counts include both false positives.
- Report search combines with verdict filters. Reset clears search, filtering
  and graph selection. Keyboard shortcuts preserve normal link and form use.
- Responsive layouts stack the scanner and reports; phone inputs use 16px text.
  Final device layout and touch inspection are still required.

## Verification

The web/report/security suite passed **42 tests**. The HTTP smoke check passed
landing/security headers, a saved fixture, public-repository scan, HTML and JSON.
These checks do not establish public hosting, layout quality or an accessibility
score.

An isolated LinkeDOM harness checked actual served markup and current scripts:
theme persistence, unavailable storage, motion toggle, reduced motion, shorthand
normalization, duplicate submission, result navigation, inline validation and
recovery, combined search/filter, reset, tabs and link keyboard behavior.
Reproduce from the repository root with a running server:

```powershell
npm install --prefix .docs-drift/ui-check --cache .npm-cache --no-audit --no-fund --ignore-scripts linkedom@0.18.12
node scripts/check_ui.cjs .docs-drift/ui-check/node_modules/linkedom http://127.0.0.1:8080
```

LinkeDOM is an optional local QA dependency, not shipped to app visitors. This
harness does not render CSS, canvas or the graph. Browser inventory was empty
even after the owner agreed to connect it; no screenshots, viewport-overflow
result, Lighthouse score or completed visual QA are claimed for this redesign.

When a browser is connected, inspect 360px, 820px and 1440px layouts in both
themes: full-page overflow, header fit, evidence-card overlap, scanner submission,
graph colors/filtering, keyboard focus, motion toggle and reduced-motion mode.

## Competition and submission limits

This improves the immediate judge path; it cannot establish a win over Debrief.
Public AWS hosting, an authentic Flow-use clip, actual dictated build prompts,
the final under-two-minute video and independent human accuracy labels remain
separate completion requirements. Usage-card account totals are not project-only
dictation statistics.
