# Podcast clips workflow

**Maturity:** candidate-handoff

## Route here when

The input is one podcast episode—possibly split across multiple files—and the intended output is several short, independently understandable clips for social distribution.

## Editorial outcome

Find moments that work as hook, minimum necessary context, and payoff. Keep clips short enough to hold attention without using deceptive non-contiguous stitching or stripping away qualifications that change the speaker's meaning. The current flat workspace may render one approved clip per run; the remaining candidates stay as an editor-ready handoff.

## Intake

Establish:

- Explicit part order when the episode spans multiple files
- Speaker identity map
- Desired number and approximate length of clips
- Platform, audience, campaign objective, and aspect ratio
- Must-promote themes, spoilers, prohibited claims, and fact-check concerns
- Brand, caption, title, and call-to-action direction

Treat split files as one editorial unit only after their order is confirmed. File-relative timestamps remain authoritative even after an episode order exists.

## Agent judgment

The agent decides which moments can stand alone, how much setup is required, where the emotional or intellectual payoff ends, and whether combining separated statements would mislead. Preserve literal quotes and flag uncertain factual claims instead of cleaning them into certainty.

Do not replace editorial reasoning with a hand-tuned virality score.

## Deterministic engine responsibilities

- Preserve filename, exact source range, quote, speaker, and EDL traceability
- Preserve transcript word times and compute output-timeline caption offsets
- Render at most one approved clip in the current flat run workspace
- Produce caption, audio, duration, and cut-boundary QC evidence for that clip

## Lifecycle and approvals

1. Confirm part order and clip-selection criteria.
2. Read packed transcripts across the whole editorial unit in bounded sections.
3. Write `edit/clip_candidates.md` with primary and backup pulls: literal source file, exact source-relative timestamp, exact quote, why it works, required context, and any fact-check or decontextualization risk.
4. Wait for candidate approval before creating clip EDLs.
5. Obtain approval for one representative framing and caption treatment.
6. Select at most one candidate to render in the current run and obtain rough-cut approval.
7. Render that approved clip and record its artifacts in `edit/run.json`. Leave the remaining candidates as handoff rows until namespaced runs exist.

## Deliverables

- `edit/clip_candidates.md`
- One approved clip EDL and source traceability for the current run
- One preview, final, and SRT when requested
- Optional title, caption, and CTA suggestions
- QC evidence and accepted exceptions

Every editor-facing row should use literal filenames and exact source-relative timestamps.

## Complete when

The candidate handoff is complete when every row maps to an exact source range and identifies context or fact-check risk. If one clip is rendered, it is understandable without deceptive context loss and passes output and caption QC.

## Current limits

EDL v1 can cut homogeneous sequential sources, but the engine does not yet provide namespaced multi-output runs, target-canvas reframing, mixed-aspect concat, or active-speaker tracking. Do not automate or claim a rendered batch from the current flat workspace.
