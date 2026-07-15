# Podcast editing workflow

**Maturity:** handoff-first

## Route here when

The intended outcome is a coherent long-form episode rather than short promotional clips. The material may be one mixed master or several confirmed sequential parts.

## Honest scope

Current support is editor-ready content planning plus simple rough cuts from a mixed master or homogeneous sequential sources. Do not claim multicam, isolated-track mixing, J/L cuts, NLE interchange, or picture-lock delivery until a v2 timeline supports them.

## Editorial outcome

Remove logistics, false starts, unusable sections, dead air, and true repetition while preserving conversational cadence, nuance, reactions, and the speaker's intended meaning. Identify chapters and fact-check risks without turning uncertain statements into clean claims.

## Intake

Establish:

- Literal source files and explicit part order
- Whether sources are sequential parts, cameras, isolated microphones, or a mixed master
- Speaker map, synchronization evidence, and known clean content boundaries
- Intro, outro, ads, music, screen recordings, and B-roll
- Must-keep, must-cut, fact-check, and sensitivity notes
- Target shape, runtime, delivery format, and intended editor handoff

Never infer combined timing merely from filename order. If the sources require multicam or separate-track synchronization, stop at a verified handoff unless the missing capability is implemented and tested.

## Agent judgment

The agent decides which tangents or repetitions can be removed, how much conversational air and reaction to retain, where chapters belong, and which uncertain references require fact-checking. Substantive cuts must remain reversible and explain why the episode improves without changing meaning.

## Deterministic engine responsibilities

- Preserve literal filename and exact source-relative timestamps
- Maintain an explicitly confirmed part and sync map
- Validate all cut edges and derived output timings
- Measure audio and media properties without making editorial decisions
- Export reversible edit data and QC evidence within supported scope

## Lifecycle and approvals

1. Verify source roles, order, synchronization, and clean content boundaries.
2. Write `edit/cutlist.md` with primary cuts, optional tightening, chapter markers, fact-check flags, literal filenames, exact ranges, exact quotes, and rationale.
3. Obtain approval for the narrative plan and substantive cut list.
4. When the sources fit EDL v1, create a reversible rough-cut EDL and review preview.
5. Obtain picture/content-lock approval before final rendering.
6. Obtain separate approval for mix and caption treatment when applicable.

## Deliverables

Handoff-first scope:

- Verified source/part map
- `edit/cutlist.md`
- Chapter outline and fact-check flags
- Optional EDL v1 rough cut for supported sources

Future NLE-grade scope:

- Reversible multicam timeline
- Audio master and stems
- SRT/VTT and chapter mapping
- OTIO/FCPXML plus marker CSV

## Complete when

For a handoff, source order is verified and every recommendation has an exact file-relative range, quote, category, and rationale. For a supported rough cut, the preview and QC pass and all substantive changes have approval. Never mark a multicam or separate-track project complete when only a flattened MP4 exists.

## Current limits

The renderer currently assumes one audio stream per source, homogeneous concat-compatible segments, source-derived aspect, and 24 fps output. It has no multicam sync, independent audio/video tracks, J/L cuts, audio stems, or NLE interchange.
