# Responsibility
Stop target activity now. This conclude instruction overrides earlier verification instructions. Decide only from the independent attempt and evidence already available for the bound Observation. Do not run another reproduction, contact a Target, wait, discover unrelated issues, or broaden scope.

# Output Requirements
Call the SDK `submit_result` tool with `accepted:true` and the business fields allowed by its supplied Schema, or `accepted:false` with a non-empty `reason`. Submit concise `data.description` and `data.verification_result` with one Schema outcome and non-empty current-run evidence. Execution failure, cancellation, timeout, provider failure, or an unstarted run is not a verification outcome.

Call `register_artifact` only for already available worker-local files using their existing absolute `path` and `kind`, then use returned handles in `evidence_refs[].artifact_handle`. Prior evidence cannot substitute for independent current-run evidence. Do not invent handles or Artifact IDs, and do not submit an ordinary assessment record or a new vulnerability candidate. Correction may only repair the submission from existing evidence.

# Neutral Bound Observation
ID: `{target_observation_id}`
```json
{verifier_target}
```
# Intent
`{intent_id}` — {intent_description}
# Knowledge
```json
{knowledge_base}
```
