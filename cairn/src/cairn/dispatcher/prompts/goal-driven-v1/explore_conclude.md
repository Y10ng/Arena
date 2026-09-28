# Responsibility
Stop target activity now. This conclude instruction overrides earlier exploration instructions. Conclude only the bounded experiment already performed for the Current Intent. Do not run another variant, contact a Target, wait for unfinished work, or expand scope. Preserve actual preconditions, baseline, discriminating variants, controls, observations, impact, attempted strategies, and limitations. Unperformed tests remain untested.

Submit one authoritative `assessment_record`; do not submit a second experiment copy. Follow its supplied Schema for the outcome and blocker fields. A tested-no-finding outcome needs actual evidence. A finding needs demonstrated impact and stable vulnerability identity. A blocked or not-tested outcome must state the real limitation and cannot be synthesized from an execution failure.

# Output Requirements
Call the SDK `submit_result` tool with `accepted:true` and business fields allowed by its supplied Schema, or `accepted:false` with a non-empty `reason`. Submit a concise `data.fact` and the single `data.assessment_record` when an experiment occurred. Do not submit `coverage_result`, server-assigned identities, or a completion decision.

Use only existing evidence. For an already available file, call `register_artifact` with its absolute worker-local `path` and `kind`, then cite the returned handle in `evidence_refs[].artifact_handle`. Evidence may include prior `curl --trace-ascii <path>` output or available browser network output/screenshots saved in worker-local files. Prefer `packet_artifact_handle` for a complete exact packet. Screenshots or partial network logs are not complete HTTP packets; preserve limitations without inventing headers or responses. Do not invent handles or Artifact IDs. Correction may only repair the submission from existing evidence.

{surface_discovery_contract}

# Goal
```
{goal}
```
# Authorized Targets
```json
{authorized_targets}
```
# Current Intent
`{intent_id}` — {intent_description}
# Graph and Evidence Index (read on demand)
```
{graph_yaml}
```
# Knowledge
```json
{knowledge_base}
```
