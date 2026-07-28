from enum import Enum


class Profile(str, Enum):
    INSPECT = "inspect"
    WORKFLOW = "workflow"
    EDIT = "edit"
    FULL = "full"


class ApprovalMode(str, Enum):
    CLI = "cli"
    CLIENT = "client"


class ToolClass(str, Enum):
    INSPECT = "inspect"
    OFFLINE_WRITE = "offline_write"
    STATEFUL_WRITE = "stateful_write"
    LIVE_READ = "live_read"
    LIVE_WRITE = "live_write"
    WORKFLOW = "workflow"


PROFILE_TOOL_CLASSES = {
    Profile.INSPECT: {ToolClass.INSPECT},
    Profile.WORKFLOW: {ToolClass.INSPECT, ToolClass.WORKFLOW},
    Profile.EDIT: {
        ToolClass.INSPECT,
        ToolClass.OFFLINE_WRITE,
        ToolClass.STATEFUL_WRITE,
        ToolClass.WORKFLOW,
    },
    Profile.FULL: set(ToolClass),
}
