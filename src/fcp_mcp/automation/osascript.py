from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from subprocess import CompletedProcess
from typing import Literal

from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import ErrorCode, FCPMCPError

_MODIFIER_ALIASES = {
    "cmd": "command",
    "command": "command",
    "shift": "shift",
    "opt": "option",
    "option": "option",
    "alt": "option",
    "ctrl": "control",
    "control": "control",
}
_SPECIAL_KEYS = frozenset(
    {"space", "return", "escape", "left", "right", "up", "down"}
)


@dataclass(frozen=True)
class OsaProgram:
    language: Literal["AppleScript", "JavaScript"]
    source: str

    def __post_init__(self) -> None:
        if self.language not in {"AppleScript", "JavaScript"}:
            raise FCPMCPError(
                ErrorCode.INVALID_ARGUMENTS,
                f"Unsupported osascript language: {self.language}",
            )


FCP_LIBRARIES = OsaProgram(
    "JavaScript",
    """
function run(argv) {
    const fcp = Application("Final Cut Pro");
    const libraries = fcp.libraries();
    const result = [];
    for (let index = 0; index < libraries.length; index++) {
        result.push({
            name: libraries[index].name(),
            id: libraries[index].id(),
            file: libraries[index].file().toString()
        });
    }
    return JSON.stringify(result);
}
""".strip(),
)

FCP_EVENTS = OsaProgram(
    "JavaScript",
    """
function run(argv) {
    const libraryName = argv.length ? argv[0] : "";
    const fcp = Application("Final Cut Pro");
    const libraries = fcp.libraries();
    const result = [];
    for (let libraryIndex = 0; libraryIndex < libraries.length; libraryIndex++) {
        if (libraryName && libraries[libraryIndex].name() !== libraryName) {
            continue;
        }
        const events = libraries[libraryIndex].events();
        for (let eventIndex = 0; eventIndex < events.length; eventIndex++) {
            result.push({
                library: libraries[libraryIndex].name(),
                name: events[eventIndex].name(),
                id: events[eventIndex].id()
            });
        }
    }
    return JSON.stringify(result);
}
""".strip(),
)

FCP_PROJECTS = OsaProgram(
    "JavaScript",
    """
function run(argv) {
    const eventName = argv.length ? argv[0] : "";
    const fcp = Application("Final Cut Pro");
    const libraries = fcp.libraries();
    const result = [];
    for (let libraryIndex = 0; libraryIndex < libraries.length; libraryIndex++) {
        const events = libraries[libraryIndex].events();
        for (let eventIndex = 0; eventIndex < events.length; eventIndex++) {
            if (eventName && events[eventIndex].name() !== eventName) {
                continue;
            }
            const projects = events[eventIndex].projects();
            for (let projectIndex = 0; projectIndex < projects.length; projectIndex++) {
                const sequence = projects[projectIndex].sequence();
                const duration = sequence ? sequence.duration() : null;
                result.push({
                    library: libraries[libraryIndex].name(),
                    event: events[eventIndex].name(),
                    name: projects[projectIndex].name(),
                    id: projects[projectIndex].id(),
                    duration: duration
                        ? duration.value + "/" + duration.timescale + "s"
                        : "unknown"
                });
            }
        }
    }
    return JSON.stringify(result);
}
""".strip(),
)

FCP_TIMELINE_INFO = OsaProgram(
    "JavaScript",
    """
function run(argv) {
    const fcp = Application("Final Cut Pro");
    const libraries = fcp.libraries();
    if (libraries.length === 0) {
        return JSON.stringify({error: "No libraries open"});
    }
    const events = libraries[0].events();
    if (events.length === 0) {
        return JSON.stringify({error: "No events"});
    }
    const projects = events[0].projects();
    if (projects.length === 0) {
        return JSON.stringify({error: "No projects"});
    }
    const project = projects[0];
    const sequence = project.sequence();
    return JSON.stringify({
        library: libraries[0].name(),
        event: events[0].name(),
        project: project.name(),
        duration: sequence ? {
            value: sequence.duration().value,
            timescale: sequence.duration().timescale
        } : null,
        frameDuration: sequence ? {
            value: sequence.frameDuration().value,
            timescale: sequence.frameDuration().timescale
        } : null,
        tcFormat: sequence ? sequence.timecodeFormat() : null
    });
}
""".strip(),
)

FCP_APP_STATE = OsaProgram(
    "JavaScript",
    """
function run(argv) {
    const fcp = Application("Final Cut Pro");
    return JSON.stringify({
        name: fcp.name(),
        version: fcp.version(),
        frontmost: fcp.frontmost(),
        libraryCount: fcp.libraries().length
    });
}
""".strip(),
)

FCP_EXPORT_XML = OsaProgram(
    "AppleScript",
    """
on run argv
    tell application "Final Cut Pro" to activate
    delay 0.5
    tell application "System Events"
        tell process "Final Cut Pro"
            click menu item "Export XML..." of menu "File" of menu bar 1
        end tell
    end tell
    return "Export XML dialog opened"
end run
""".strip(),
)

FCP_PLAYBACK = OsaProgram(
    "AppleScript",
    """
on run argv
    set actionName to item 1 of argv
    set keyName to item 2 of argv
    tell application "Final Cut Pro" to activate
    delay 0.3
    tell application "System Events"
        if actionName is "toggle" then
            key code 49
        else
            keystroke keyName
        end if
    end tell
    return "Playback: " & actionName
end run
""".strip(),
)

FCP_NAVIGATE = OsaProgram(
    "AppleScript",
    """
on run argv
    set originalTimecode to item 1 of argv
    set cleanTimecode to item 2 of argv
    tell application "Final Cut Pro" to activate
    delay 0.3
    tell application "System Events"
        tell process "Final Cut Pro"
            key code 35 using control down
            delay 0.3
            keystroke cleanTimecode
            delay 0.1
            key code 36
        end tell
    end tell
    return "Navigated to " & originalTimecode
end run
""".strip(),
)

FCP_SELECT_TOOL = OsaProgram(
    "AppleScript",
    """
on run argv
    set toolName to item 1 of argv
    set keyName to item 2 of argv
    tell application "Final Cut Pro" to activate
    delay 0.2
    tell application "System Events" to keystroke keyName
    return "Tool: " & toolName
end run
""".strip(),
)

FCP_UNDO = OsaProgram(
    "AppleScript",
    """
on run argv
    tell application "Final Cut Pro" to activate
    delay 0.2
    tell application "System Events" to keystroke "z" using command down
    return "Undo performed"
end run
""".strip(),
)

FCP_REDO = OsaProgram(
    "AppleScript",
    """
on run argv
    tell application "Final Cut Pro" to activate
    delay 0.2
    tell application "System Events" to keystroke "z" using {command down, shift down}
    return "Redo performed"
end run
""".strip(),
)

FCP_MENU_COMMAND = OsaProgram(
    "AppleScript",
    """
on run argv
    set argumentCount to count argv
    if argumentCount is not 4 and argumentCount is not 5 then
        error "Expected validated menu path arguments"
    end if
    set originalPath to item 2 of argv
    set menuName to item 3 of argv
    set parentItemName to item 4 of argv
    tell application "Final Cut Pro" to activate
    delay 0.5
    tell application "System Events"
        tell process "Final Cut Pro"
            if argumentCount is 4 then
                click menu item parentItemName of menu menuName of menu bar 1
            else
                set childItemName to item 5 of argv
                click menu item childItemName of menu 1 of menu item parentItemName of menu menuName of menu bar 1
            end if
        end tell
    end tell
    return "Executed: " & originalPath
end run
""".strip(),
)

FCP_KEYBOARD_SHORTCUT = OsaProgram(
    "AppleScript",
    """
on run argv
    set originalShortcut to item 1 of argv
    set keyName to item 2 of argv
    set modifierList to {}
    if (count argv) > 2 then
        repeat with argumentIndex from 3 to count argv
            set modifierName to item argumentIndex of argv
            if modifierName is "command" then
                set end of modifierList to command down
            else if modifierName is "shift" then
                set end of modifierList to shift down
            else if modifierName is "option" then
                set end of modifierList to option down
            else if modifierName is "control" then
                set end of modifierList to control down
            end if
        end repeat
    end if
    tell application "Final Cut Pro" to activate
    delay 0.2
    tell application "System Events"
        if keyName is "space" then
            key code 49 using modifierList
        else if keyName is "return" then
            key code 36 using modifierList
        else if keyName is "escape" then
            key code 53 using modifierList
        else if keyName is "left" then
            key code 123 using modifierList
        else if keyName is "right" then
            key code 124 using modifierList
        else if keyName is "down" then
            key code 125 using modifierList
        else if keyName is "up" then
            key code 126 using modifierList
        else
            keystroke keyName using modifierList
        end if
    end tell
    return "Shortcut sent: " & originalShortcut
end run
""".strip(),
)

FCP_SHARE = OsaProgram(
    "AppleScript",
    """
on run argv
    tell application "Final Cut Pro" to activate
    delay 0.5
    tell application "System Events"
        tell process "Final Cut Pro"
            if (count argv) > 0 and item 1 of argv is not "" then
                set destinationName to item 1 of argv
                click menu item destinationName of menu 1 of menu item "Share" of menu "File" of menu bar 1
                return "Share triggered: " & destinationName
            end if
            click menu item "Share" of menu "File" of menu bar 1
        end tell
    end tell
    return "Share menu opened"
end run
""".strip(),
)


def require_live_control(config: RuntimeConfig) -> None:
    if not config.live_control_enabled:
        raise FCPMCPError(
            ErrorCode.LIVE_CONTROL_DISABLED,
            "Set FCP_MCP_ENABLE_LIVE_CONTROL=1 to use live FCP or Compressor tools",
        )


def parse_shortcut(keys: str) -> tuple[str, tuple[str, ...]]:
    tokens = [token.strip().lower() for token in keys.split("+")]
    if not keys.strip() or any(not token for token in tokens):
        raise FCPMCPError(
            ErrorCode.INVALID_ARGUMENTS,
            "Shortcut must contain one key and optional modifiers",
        )

    modifiers: list[str] = []
    key_tokens: list[str] = []
    for token in tokens:
        modifier = _MODIFIER_ALIASES.get(token)
        if modifier is None:
            key_tokens.append(token)
        elif modifier in modifiers:
            raise FCPMCPError(
                ErrorCode.INVALID_ARGUMENTS,
                f"Duplicate shortcut modifier: {modifier}",
            )
        else:
            modifiers.append(modifier)

    if len(key_tokens) != 1:
        raise FCPMCPError(
            ErrorCode.INVALID_ARGUMENTS,
            "Shortcut must contain exactly one key",
        )
    key = key_tokens[0]
    if not ((len(key) == 1 and key.isascii() and key.isalnum()) or key in _SPECIAL_KEYS):
        raise FCPMCPError(
            ErrorCode.INVALID_ARGUMENTS,
            f"Unsupported shortcut key: {key}",
        )
    return key, tuple(modifiers)


def run_osascript(
    program: OsaProgram,
    args: Sequence[str] = (),
    *,
    timeout: float = 10,
    config: RuntimeConfig,
    runner: Callable[..., CompletedProcess[str]] = subprocess.run,
) -> str:
    require_live_control(config)
    command = [
        "osascript",
        "-l",
        program.language,
        "-e",
        program.source,
        *(str(argument) for argument in args),
    ]
    try:
        result = runner(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise FCPMCPError(
            ErrorCode.COMMAND_FAILED,
            f"osascript timed out after {timeout:g} seconds",
        ) from error
    except FileNotFoundError as error:
        raise FCPMCPError(
            ErrorCode.DEPENDENCY_MISSING,
            "osascript executable was not found",
        ) from error
    except PermissionError as error:
        raise FCPMCPError(
            ErrorCode.PERMISSION_DENIED,
            f"osascript could not be executed: {error}",
        ) from error
    except OSError as error:
        raise FCPMCPError(
            ErrorCode.COMMAND_FAILED,
            f"osascript could not be executed: {error}",
        ) from error

    if result.returncode:
        stderr = (result.stderr or "").strip()
        denied = any(
            marker in stderr.lower()
            for marker in ("not authorized", "not permitted", "assistive", "accessibility")
        )
        code = ErrorCode.PERMISSION_DENIED if denied else ErrorCode.COMMAND_FAILED
        raise FCPMCPError(code, stderr or "osascript failed")
    return (result.stdout or "").strip()
