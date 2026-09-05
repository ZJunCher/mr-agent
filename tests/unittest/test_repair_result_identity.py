from types import SimpleNamespace

from pr_agent.triage.repair_details import RepairAction
from pr_agent.triage.repair_result_identity import resolve_repair_result_identity


def test_pipeline_without_repair_commit_is_evidence_only():
    identity = resolve_repair_result_identity(
        None,
        (),
        current_pipeline_id=34796,
        current_pipeline_sha="source-sha",
        current_pipeline_status="failed",
    )

    assert identity.exists is False
    assert identity.pipeline_id == 0
    assert identity.commit_sha == ""


def test_legacy_repair_action_proves_exact_result_pipeline():
    action = RepairAction(
        commit_sha="repair-sha",
        validation_pipeline_id=34801,
        validation_status="success",
    )

    identity = resolve_repair_result_identity(
        None,
        (action,),
        current_pipeline_id=34802,
        current_pipeline_sha="later-sha",
        current_pipeline_status="failed",
    )

    assert identity.exists is True
    assert identity.pipeline_id == 34801
    assert identity.commit_sha == "repair-sha"
    assert identity.pipeline_status == "success"


def test_unvalidated_commit_does_not_claim_unrelated_pipeline_as_result():
    action = RepairAction(commit_sha="repair-sha")

    identity = resolve_repair_result_identity(
        None,
        (action,),
        current_pipeline_id=34796,
        current_pipeline_sha="source-sha",
        current_pipeline_status="failed",
    )

    assert identity.exists is False


def test_manifest_commit_can_use_current_exact_pipeline():
    manifest = SimpleNamespace(entries=(SimpleNamespace(commit_sha="repair-sha"),))

    identity = resolve_repair_result_identity(
        manifest,
        (),
        current_pipeline_id=34801,
        current_pipeline_sha="repair-sha",
        current_pipeline_status="failed",
    )

    assert identity.exists is True
    assert identity.pipeline_id == 34801
    assert identity.commit_sha == "repair-sha"
    assert identity.pipeline_status == "failed"
