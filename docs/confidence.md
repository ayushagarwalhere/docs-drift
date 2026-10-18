# Finding confidence

These are fixed rule scores, not calibrated probabilities of correctness.
Extraction confidence describes whether text expresses a claim; finding
confidence describes the static evidence used to decide that claim.

| Evidence | Finding confidence |
| --- | --- |
| Exact indexed declaration or contained path | 0.95 |
| Missing declaration with partial language coverage | 0.50 |
| Missing declaration without language coverage | 0.20, unverifiable |
| Incorrect route method with a matching path | 0.80 |
| Clearly separated fuzzy rename candidate | score / 100, capped at 0.94 |
| Explicit, uniquely indexed literal default | 0.95 |
| Ambiguous default or incompatible units | 0.30 or 0.35, unverifiable |

Command validation, package fallback and Git history apply their own evidence
rules. Read the reasons and source links in each report. The default check
threshold is 0.60. Human-labeled benchmark results measure actual precision;
the confidence score alone does not establish it.
