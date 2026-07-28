from fcp_mcp.profiles import PROFILE_TOOL_CLASSES, ApprovalMode, Profile, ToolClass


def test_profile_membership_is_fixed():
    assert PROFILE_TOOL_CLASSES[Profile.INSPECT] == {ToolClass.INSPECT}
    assert PROFILE_TOOL_CLASSES[Profile.WORKFLOW] == {
        ToolClass.INSPECT,
        ToolClass.WORKFLOW,
    }
    assert PROFILE_TOOL_CLASSES[Profile.EDIT] == {
        ToolClass.INSPECT,
        ToolClass.OFFLINE_WRITE,
        ToolClass.STATEFUL_WRITE,
        ToolClass.WORKFLOW,
    }
    assert PROFILE_TOOL_CLASSES[Profile.FULL] == set(ToolClass)


def test_profile_and_approval_values_are_stable():
    assert [profile.value for profile in Profile] == [
        "inspect",
        "workflow",
        "edit",
        "full",
    ]
    assert [mode.value for mode in ApprovalMode] == ["cli", "client"]
