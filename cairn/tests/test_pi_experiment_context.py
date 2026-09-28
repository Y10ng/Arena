"""深测上下文只取已接受决策中的当前实验，不猜测缺失绑定。"""

import json
from types import SimpleNamespace

import pytest

from cairn.dispatcher.tasks.explore_context import explore_prompt_replacements, selected_experiment_context
from cairn.server.models import Fact, Intent


def inputs():
    experiment = {
        "from": ["f1"], "description": "Compare state replay", "slot_key": "cell1",
        "strategy_key": "deep.state.v1", "hypothesis": "Replay changes completed state",
        "preconditions": ["Authorized owner and completed order"],
        "baseline": "Owner completes order once", "variants": ["Replay completed operation"],
        "controls": ["Fresh order succeeds once"], "impact": "State integrity",
        "limitations": ["Only supplied identities"], "evidence_refs": [{"artifact_id": "a1", "pointer": ""}],
    }
    task = Intent(
        id="i1", **{"from": ["f1", "review"]}, description="Compare state replay",
        creator="reason", created_at="2026-09-16", slot_key="cell1",
        strategy_key="deep.state.v1", depth_review_fact_id="review",
    )
    fact = Fact(id="review", description="Selected experiments", assessment_record={
        "version": 1, "kind": "depth_review", "selected": [experiment],
    })
    return task, fact, experiment


def test_prompt_includes_current_experiment_not_other_hypotheses():
    task, fact, experiment = inputs()
    fact.assessment_record["selected"].append({**experiment, "strategy_key": "another", "hypothesis": "unrelated"})
    manager = SimpleNamespace(write_text_file=lambda *args: None)
    result = explore_prompt_replacements(
        manager, "owned-container", "facts: []", task,
        phase="explore_execute", surface_manifest=True, facts=[fact],
    )["intent_description"]
    projected = json.loads(result.split("\n")[-1])
    for field in ("hypothesis", "preconditions", "baseline", "variants", "controls", "impact", "limitations", "evidence_refs"):
        assert projected[field] == experiment[field]
    assert "unrelated" not in result
    assert "blocked, not a negative" in result


@pytest.mark.parametrize("fault", [
    "missing_fact", "duplicate_fact", "unbound_fact", "version", "kind", "no_selected",
    "wrong_cell", "wrong_strategy", "duplicate_selection", "wrong_source", "local_handle", "missing_controls",
])
def test_invalid_selection_never_falls_back_to_free_exploration(fault):
    task, fact, experiment = inputs()
    facts = [fact]
    if fault == "missing_fact":
        facts = []
    elif fault == "duplicate_fact":
        facts.append(fact)
    elif fault == "unbound_fact":
        task.from_ = ["f1"]
    elif fault == "version":
        fact.assessment_record["version"] = 0
    elif fault == "kind":
        fact.assessment_record["kind"] = "experiment"
    elif fault == "no_selected":
        fact.assessment_record.pop("selected")
    elif fault == "wrong_cell":
        experiment["slot_key"] = "other"
    elif fault == "wrong_strategy":
        experiment["strategy_key"] = "other"
    elif fault == "duplicate_selection":
        fact.assessment_record["selected"].append(experiment)
    elif fault == "wrong_source":
        experiment["from"] = ["unbound-source"]
    elif fault == "local_handle":
        experiment["evidence_refs"] = [{"artifact_handle": "local-candidate"}]
    elif fault == "missing_controls":
        experiment["controls"] = []
    with pytest.raises(ValueError):
        selected_experiment_context(task, facts)


def test_baseline_has_no_invented_experiment():
    task, _, _ = inputs()
    task.depth_review_fact_id = None
    assert selected_experiment_context(task, []) == task.description
