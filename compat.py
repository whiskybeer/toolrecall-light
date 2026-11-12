"""Detect a co-installed ToolRecall core (full stack) and explain the overlap.

ToolRecall core and this plugin both rewrite tool results the agent is about to
receive, at different layers:

  * core: file content on the MCP bridge / shim, plus the forward proxy that
    rewrites the request before it is billed.
  * this plugin: any tool result through ``transform_tool_result``.

They do not crash each other, but on a bridge-routed file read core sends full
content, this plugin replaces it with its own stub, and core then believes the
agent holds bytes it never saw. Running one is the supported configuration.

Detection is best-effort and read-only: nothing here raises, blocks, or changes
what the hook does. It exists so "install one, not both" is answerable from the
host log and from ``status()`` instead of from a support thread.
"""

from __future__ import annotations

import builtins
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PROXY_PORT_MARKER = "8569"
CORE_MCP_MARKER = "toolrecall"


def hermes_home() -> Path:
    """The profile-safe Hermes root this plugin stores under."""
    return Path(os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes"))


def _config_text(home: Path) -> str:
    try:
        return (home / "config.yaml").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def detect(hermes_home_path: Path | None = None) -> dict[str, Any]:
    """Return ``{"core_present": bool, "findings": [str, ...]}``. Never raises."""
    findings: list[str] = []
    try:
        home = Path(hermes_home_path) if hermes_home_path is not None else hermes_home()
        text = _config_text(home)

        # 1. Forward proxy wiring — the strongest signal: Hermes would be billing
        #    through core's proxy, which already rewrites this request.
        if PROXY_PORT_MARKER in text:
            findings.append(f"config.yaml routes LLM traffic through the ToolRecall proxy (:{PROXY_PORT_MARKER})")

        # 2. MCP bridge — core's file-read dedup surface.
        if "mcp_servers" in text and CORE_MCP_MARKER in text:
            findings.append("config.yaml registers the ToolRecall MCP bridge (mcp_servers.toolrecall)")

        # 3. Shim — core's native-open dedup, visible as a patched builtins.open.
        if getattr(builtins.open, "__name__", "") == "_shim_open":
            findings.append("the ToolRecall shim is patched into this interpreter (builtins.open)")

        # 4. Package installed for this interpreter, even if not wired yet.
        try:
            import importlib.util

            if importlib.util.find_spec("toolrecall") is not None:
                findings.append("the toolrecall package is importable in this interpreter")
        except (ImportError, ValueError, AttributeError):
            pass
    except Exception as err:  # noqa: BLE001 — detection must never break registration
        logger.debug("core detection failed: %s", err)
        return {"core_present": False, "findings": []}

    return {"core_present": bool(findings), "findings": findings}


def is_daemon_socket_active(socket_path: str | Path | None = None) -> bool:
    """Return True if a ToolRecall daemon UDS socket is actively listening."""
    import socket

    paths: list[Path] = []
    if socket_path is not None:
        paths.append(Path(socket_path))
    else:
        env_path = os.environ.get("TOOLRECALL_UDS_PATH")
        if env_path:
            paths.append(Path(env_path))
        paths.append(Path("/tmp/toolrecall.sock"))
        paths.append(Path.home() / ".toolrecall" / "toolrecall.sock")

    for p in paths:
        if p.exists():
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(0.1)
                s.connect(str(p))
                s.close()
                return True
            except OSError:
                pass
    return False


def get_coexistence_state(hermes_home_path: Path | None = None) -> dict[str, Any]:
    """Return coexistence diagnostics including socket availability."""
    base = detect(hermes_home_path)
    daemon_online = is_daemon_socket_active()
    return {
        "core_present": base["core_present"],
        "findings": base["findings"],
        "daemon_online": daemon_online,
        "coexistence_mode": "daemon_coexisting" if (base["core_present"] or daemon_online) else "standalone",
    }


def warn_if_core_present(hermes_home_path: Path | None = None) -> dict[str, Any]:
    """Log one actionable warning when a full stack is detected alongside us."""
    result = detect(hermes_home_path)
    if result["core_present"]:
        logger.warning(
            "toolrecall-light: ToolRecall core also appears active here (%s). "
            "Run one, not both — core already dedups bridge/shim file reads and its "
            "proxy rewrites the request, so both layers stubbing the same result makes "
            "core's counts overstate what the model saw. "
            "Keep core: `hermes plugins disable toolrecall-light`. "
            "Keep Light: `toolrecall stop` (or remove the proxy/MCP wiring).",
            "; ".join(result["findings"]),
        )
    return result
