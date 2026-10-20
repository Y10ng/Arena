# Responsibility
Stop target activity now. This conclude instruction overrides earlier exploration instructions. Conclude only the bounded experiment already performed for the Current Intent. Do not run another variant, contact a Target, wait for unfinished work, or expand scope. Preserve actual preconditions, baseline, discriminating variants, controls, observations, impact, attempted strategies, and limitations. Unperformed tests remain untested.

Submit one authoritative `assessment_record`; do not submit a second experiment copy. Follow its supplied Schema for the outcome and blocker fields. A tested-no-finding outcome needs actual evidence. A finding needs demonstrated impact and stable vulnerability identity. A blocked or not-tested outcome must state the real limitation and cannot be synthesized from an execution failure.

# Output Requirements
Call the SDK `submit_result` tool with `accepted:true` and business fields allowed by its supplied Schema, or `accepted:false` with a non-empty `reason`. Submit a concise `data.fact` and the single `data.assessment_record` when an experiment occurred. Do not submit `coverage_result`, server-assigned identities, or a completion decision.

Use only existing evidence. For a finding, submit `vulnerability.schema_version:2` and ordered `vulnerability.evidence_items`; reuse items already returned by `capture_http_evidence`, but do not call it or contact the target during conclude. Existing worker-local files may be registered with `register_artifact` (`path` and `kind`) and referenced by the returned handle: HTTP uses `request`/`response` from the same recorded interaction, while screenshots, logs and other files use `kind:artifact` with `artifact`. Keep missing sides and limitations; never reconstruct packets or invent handles or Artifact IDs. General experiment or non-finding Fact `evidence_refs` remain separate; do not duplicate finding evidence in Fact `evidence_refs` or submit retired packet fields. Correction only organizes existing material and repairs the submission.

{surface_discovery_contract}

# Authorization Scope
```json
{authorization_scope}
```
# Origin
```
{origin}
```
# Goal
```
{goal}
```
# Structured Target Index
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
