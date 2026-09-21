"""Authorization decision and partition-resolution tests."""

from context_engine.knowledge_backend import AccessPartitionRef
from context_engine.security.policy import Action, PolicyInput, authorize


def test_policy_resolves_only_compatible_partitions():
    decision = authorize(
        PolicyInput(
            principal_id="principal-alpha",
            action=Action.CONTEXT_READ,
            space_id="space-1",
            granted_actions=frozenset({Action.CONTEXT_READ}),
            principal_audiences=frozenset({"shared", "alpha"}),
            partition_audiences=(
                (AccessPartitionRef("shared-partition"), frozenset({"shared"})),
                (AccessPartitionRef("alpha-partition"), frozenset({"alpha"})),
                (AccessPartitionRef("beta-partition"), frozenset({"beta"})),
            ),
            policy_version="7",
        )
    )
    assert decision.allowed
    assert [item.value for item in decision.partitions] == ["shared-partition", "alpha-partition"]


def test_policy_denies_missing_action():
    decision = authorize(
        PolicyInput(
            principal_id="principal-alpha",
            action=Action.RECORD_DELETE,
            space_id="space-1",
            granted_actions=frozenset({Action.CONTEXT_READ}),
            principal_audiences=frozenset({"alpha"}),
            partition_audiences=(),
            policy_version="7",
        )
    )
    assert not decision.allowed
    assert decision.reason_code == "action_not_granted"
