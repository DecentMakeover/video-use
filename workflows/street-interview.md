# Street interview workflow

**Maturity:** foundation

## Route here when

The material contains multiple short respondent answers, usually captured on phones or cameras in a public setting, and the intended output is one concise interview montage for the current run.

## Editorial outcome

Create a hook-first piece that feels like one coherent conversation: establish the question, choose strong and varied answers, preserve authentic reactions, remove repetition, and retain the energy of the location without changing what respondents meant.

## Intake

Establish:

- The recurring question or theme
- Target platform, aspect ratio, runtime, and number of deliverables
- Respondent names or anonymity requirements when known
- Must-use and must-avoid answers
- Consent, face-blur, location, or privacy concerns
- Brand, caption, framing, and audio direction

Do not upload audio for transcription until the shared external-transcription gate is approved.

## Agent judgment

The agent decides which answers are compelling, concise, distinct, and authentic; how respondents should be balanced; whether the interviewer question is needed; how much reaction tail to preserve; and whether imperfect street audio is emotionally worth keeping.

Do not use a numeric answer score or silently optimize for controversy. Do not reorder fragments in a way that changes a respondent's meaning.

## Deterministic engine responsibilities

- Preserve literal filenames and exact source-relative word ranges
- Detect rotation and HDR correctly
- Enforce word-boundary padding and audio fades
- Apply supported source-derived scaling plus the approved caption, grade, and loudness treatment
- Produce cut-boundary, subtitle, overlay, and output-property evidence

Face or noise detection may surface review candidates. It must not decide consent or editorial worth.

## Lifecycle and approvals

1. Inventory and visually sample the sources.
2. Confirm the question, theme, privacy constraints, and editorial strategy.
3. Write `edit/selects.md` with literal filename, exact timestamp, quote, respondent when known, and selection reason.
4. Wait for select approval before creating the EDL.
5. Produce one representative framing and caption sample when the treatment is new; obtain approval before applying it broadly.
6. Render and self-QC the preview.
7. Obtain preview approval before the final render or overwrite.

## Deliverables

- `edit/selects.md`
- `edit/edl.json`
- `edit/master.srt` when captions are requested
- `edit/preview.mp4` and `edit/final.mp4`
- `edit/verify/` evidence and recorded verdicts in `edit/run.json`
- Optional privacy or face-blur review list

## Complete when

The approved answers and order are traceable to exact source ranges, the question is understandable, no respondent's meaning is distorted, visual and audio QC pass, privacy exceptions are resolved, and the final artifact has explicit approval.

## Current limits

The shared renderer preserves source orientation but does not yet provide a general target-canvas or active-speaker reframing system. Mixed-aspect or multi-output projects may require sequential runs and explicit artifact preservation.
