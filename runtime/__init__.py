"""JARVIS v2 runtime layer.

Hosts the asynchronous core (Task_Manager, Tool_Runtime, Plugin_Host),
cross-cutting services (Privacy_Mode, Conversation Logger, Clipboard_Manager),
and the orchestration helpers (Routine_Engine, Hotkey Manager, Tray_Agent).
"""

from runtime.plugin_host import PluginHost
from runtime.privacy_mode import PrivacyMode
from runtime.task_manager import TaskManager
from runtime.tool_runtime import (
    DEFAULT_BACKGROUND_TOOLS,
    ToolRuntime,
    default_execution_mode_for,
)

__all__ = [
    "PluginHost",
    "PrivacyMode",
    "TaskManager",
    "ToolRuntime",
    "DEFAULT_BACKGROUND_TOOLS",
    "default_execution_mode_for",
]
