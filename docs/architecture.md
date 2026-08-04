# Workflow architecture

## Decision

Video Use has one deterministic media engine and several prompt-defined editorial workflows.

- `SKILL.md` is the shared router, lifecycle, and production-correctness contract.
- `helpers/` contains deterministic media primitives: transcription, transcript packing, inspection, grading, rendering, and QC.
- `workflows/*.md` contains editorial judgment, workflow-specific approvals, and deliverable definitions.
- `<videos_dir>/edit/` is the shared workspace observed by both the user and the agent.

Adding a workflow should usually mean adding one Markdown contract. It should not require copying the renderer, adding a Python workflow class, or registering another skill.

## Boundaries

Current deterministic helpers own:

- Media probing, orientation, and HDR detection
- Provider word timestamps and transcript packing
- Segment extraction, audio fades, concat, and output-timeline caption math
- Grade, loudness, overlay composition, and subtitle ordering
- QC frame and timeline extraction
- Cutlist validation, cut inversion into keep ranges, and derived output timings for long-form removal edits (`helpers/cutlist.py`), including word-boundary evidence from cached transcripts

Future deterministic helpers should own:

- Content fingerprints and transcript-cache validation
- Schema validation for hand-authored EDLs
- Configurable canvas, frame rate, and delivery profiles
- Namespaced multi-output runs and interchange generation

Keep editorial judgment in prompts:

- What is compelling, repetitive, misleading, or worth preserving
- Narrative shape, pacing, respondent or speaker balance
- How much context a moment needs
- Whether imperfect media is emotionally valuable
- Aesthetic direction and whether a proposed crop or treatment looks right

Helpers may surface evidence. They must not silently decide consent, editorial worth, truthfulness, or the meaning of a speaker's words.

## Workflow router

The root skill chooses zero or one workflow contract. A clear match loads one; the generic path loads none:

| Workflow | Current scope |
| --- | --- |
| Street interviews | Foundation workflow for selecting and rendering interview montages |
| Podcast clips | Candidate handoff plus one approved clip render per run; batch namespacing and active-speaker reframing remain future work |
| Podcast editing | Editor-ready cut planning and simple mixed-master rough cuts via the cutlist compiler; not yet multicam or NLE-grade |

The existing generic workflow remains available. The agent asks a routing question only when the choice materially changes the deliverables or approval gates.

## Project state

`project.md` remains the durable, human-readable memory. A run also creates `<videos_dir>/edit/run.json` for resumable machine state, starting from `workflows/run.example.json`:

```json
{
  "version": 1,
  "workflow": "generic",
  "phase": "inventory",
  "status": "active",
  "source_inventory": [],
  "approvals": {
    "external_transcription": {"required": false, "status": "not_required", "scope": []},
    "strategy": {"required": true, "status": "pending", "scope": []}
  },
  "artifacts": {},
  "checks": {},
  "attempt": 0,
  "next_action": "Inventory sources"
}
```

Allowed statuses are `active`, `waiting_for_user`, `complete`, and `blocked`. The phase is one of `inventory`, `strategy`, `selection`, `preview`, `qc`, or `final`.

Approval entries are workflow-specific. Include only applicable gates, or mark a non-applicable gate as `required: false` and `status: not_required`. Approval status is one of `pending`, `approved`, `rejected`, or `not_required`. Street selects, clip candidates, treatment samples, preview, picture lock, mix, and final render become keys only when that workflow actually requires them.

Before hosted transcription, set `external_transcription` to `required: true` and `status: pending`. Change it to `approved` with the exact source scope only after the user consents; do not call the transcription helper while it remains pending.

Approvals record their scope. On resume, re-probe each approved source's literal path, size, and modification time. If a source changed or may have been replaced, invalidate its upload approval, transcript, and downstream approvals. The current transcription helper caches only by filename stem and does not enforce fingerprints; move the stale transcript out of `edit/transcripts/` before retranscribing. Content-addressed cache validation remains deferred.

## Time model

Every editorial recommendation must preserve literal source identity and file-relative timestamps.

1. **Source time** is authoritative: `asset_id`, literal filename, `source_in`, and `source_out`.
2. **Episode time** is optional and exists only after the user confirms part order and any gaps.
3. **Output time** is derived from the approved EDL after cuts.

For split podcast recordings, treat the files as one editorial unit while reporting source-relative ranges such as:

```text
Recording 7 (1).mp4 | 00:14:30.000-00:14:49.000 | "Exact quote..."
```

Never infer a stitched timeline from filename order. EDL v1 continues to use seconds as floats for compatibility. A future v2 timeline should use integer time units plus each asset's source timebase to avoid accumulated drift.

## Approval gates

1. **External transcription:** before sending local audio to ElevenLabs, state what will be uploaded, to which provider, and why. Wait for explicit approval.
2. **Editorial strategy:** before creating or changing an EDL or starting a render, obtain approval for the plain-English plan.
3. **Workflow selections:** street-interview selects, podcast clip candidates, and substantive podcast cuts require approval before rendering.
4. **Final render or overwrite:** obtain approval after the review preview and before creating or replacing a final artifact.

Read-only probing is safe before approval. QC rerenders that stay within an already approved strategy do not require a new approval unless they change content or meaning.

## Completion and resume

Do not infer completion from the presence of `final.mp4`. Set `run.json.status` to `complete` only when:

- The expected deliverables exist and `ffprobe` succeeds on media outputs.
- Duration and stream properties match the approved plan.
- Cut-boundary, subtitle, overlay, and audio checks pass or contain an explicitly accepted exception.
- `project.md` records the decisions and remaining limitations.
- No approval or unresolved issue remains.

A preview awaiting feedback is `waiting_for_user`. After the existing three-pass self-QC cap, unresolved failures become `blocked` with evidence and a concrete next action.

At startup or resume, read `run.json`, the last `project.md` session, a fresh source inventory, and zero or one selected workflow contract. `workflow: generic` uses only the root skill. Load `takes_packed.md` only when transcript reasoning is required; long podcasts should be read in bounded sections.

## Capability parity

There is no separate UI. The filesystem is the shared interface, so users can inspect or revise the same manifests, handoffs, EDLs, previews, and reports that the agent uses.

| User outcome | Agent capability |
| --- | --- |
| Inspect sources | `ffprobe` plus on-demand timeline views |
| Obtain exact transcript ranges | Word-level transcription and packed transcripts after upload approval |
| Review the plan before editing | Plain-English strategy and workflow-specific selection handoff |
| Inspect or revise cuts | Human-readable EDL reasons and exact source-relative timestamps |
| Review visual treatment | Preview, full-resolution subtitle samples, and overlay frames |
| Resume later | `project.md`, `run.json`, cached transcripts, and source fingerprints |
| Receive final media or editor notes | Rendered outputs and workflow-specific Markdown handoffs under `edit/` |

## Agent-native checklist

| Check | Decision |
| --- | --- |
| Parity | User and agent share the `edit/` workspace and every deliverable is inspectable. |
| Granularity | Helpers remain media primitives; workflow prompts retain editorial judgment. |
| Composability | A new editorial workflow normally adds one Markdown contract. |
| Emergent capability | The generic workflow and permission to invent edits remain intact. |
| Dynamic vs static tools | The fixed local helper set is appropriate; no dynamic external API surface is currently needed. |
| CRUD completeness | Project artifacts are ordinary files the host agent can create, read, update, and remove with normal approval safeguards. |
| Primitives, not workflows | No `edit_street_interview()` or equivalent orchestration function is introduced. |
| API as validator | Not applicable yet; future schemas should avoid duplicating provider validation unnecessarily. |
| Shared workspace | Sources stay untouched; all run state and outputs live under `edit/`. |
| Context file | `project.md` holds human-readable memory; `run.json` holds machine progress. |
| File organization | Preserve the current flat layout until real multi-output projects justify namespaced runs. |
| Completion signal | The agent explicitly writes `run.json.status = complete` after verification. |
| Partial completion | Phase, checks, artifacts, attempts, and next action are persisted. |
| Context limits | Load zero or one workflow and bounded transcript sections on demand. |
| Available resources | Startup inventory injects current files, selected workflow, and prior state. |
| Available capabilities | Root `SKILL.md` documents the helper primitives in user vocabulary. |
| Dynamic context | Re-probe sources and reread state on every resume. |
| Agent to UI | Not applicable; filesystem changes are the product surface. |
| No silent actions | Approval status and resulting artifacts are written into `run.json`. |
| Capability discovery | The root router and workflow status labels state what is and is not supported. |
| Mobile checkpoint/resume | Host-managed; durable file state supports resume but no mobile runtime is provided. |
| iCloud and model-tier selection | Not applicable to this CLI/skill repository. |

## Compatibility and upstream sync

- Do not move `SKILL.md` or `helpers/`; installed skills symlink the whole repository and invoke helpers by their current paths.
- Preserve EDL v1 and current CLI defaults while new workflows are prompt-only.
- Keep upstream changes easy to merge by making workflow files additive and root-skill changes small.
- Test paths relative to the EDL parent, output-timeline caption offsets, fades, overlay timing, and subtitles-last ordering.

## Deferred capabilities

Do not claim these until they are designed and validated against real podcast footage:

- Namespaced concurrent multi-output runs
- Content-addressed transcript caching and explicit episode part manifests; the current cache is filename-stem based
- Configurable canvas, frame rate, caption chunking, and delivery profiles
- Active-speaker reframing and mixed-aspect concatenation
- Multicam synchronization, isolated audio tracks, J/L cuts, and audio stems
- OTIO/FCPXML interchange and an EDL v2 time model
- A custom orchestrator, MCP layer, database, vector index, or self-modifying workflow system
