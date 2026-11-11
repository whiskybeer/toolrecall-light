"""Tests for the ToolRecall-core co-install detector."""

import builtins
import importlib.util
import logging

import pytest

import compat


@pytest.fixture(autouse=True)
def no_core_package(monkeypatch):
    """Hermetic baseline: the dev venv may well have toolrecall importable."""
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: None)


def _home(tmp_path, monkeypatch, config: str | None = None):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    if config is not None:
        (tmp_path / "config.yaml").write_text(config, encoding="utf-8")
    return tmp_path


def test_clean_home_reports_no_core(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, config="model:\n  name: x\n")
    result = compat.detect()
    assert result == {"core_present": False, "findings": []}


def test_detects_proxy_wiring(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, config="model:\n  base_url: http://127.0.0.1:8569/v1\n")
    result = compat.detect()
    assert result["core_present"] is True
    assert any("proxy" in f for f in result["findings"])


def test_detects_mcp_bridge(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, config="mcp_servers:\n  toolrecall:\n    command: toolrecall\n")
    result = compat.detect()
    assert result["core_present"] is True
    assert any("MCP bridge" in f for f in result["findings"])


def test_detects_shim_by_open_name(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, config="model:\n  name: x\n")
    real_open = builtins.open

    def shimmed_open(*args, **kwargs):
        return real_open(*args, **kwargs)

    shimmed_open.__name__ = "_shim_open"
    monkeypatch.setattr(builtins, "open", shimmed_open)
    result = compat.detect()
    assert result["core_present"] is True
    assert any("shim" in f for f in result["findings"])


def test_detects_installed_package(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, config="model:\n  name: x\n")
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: object())
    result = compat.detect()
    assert result["core_present"] is True
    assert any("importable" in f for f in result["findings"])


def test_multiple_findings_are_all_reported(tmp_path, monkeypatch):
    _home(
        tmp_path,
        monkeypatch,
        config="model:\n  base_url: http://127.0.0.1:8569/v1\nmcp_servers:\n  toolrecall:\n    command: x\n",
    )
    findings = compat.detect()["findings"]
    assert len(findings) == 2


def test_missing_config_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert compat.detect() == {"core_present": False, "findings": []}


def test_config_is_a_directory_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").mkdir()
    assert compat.detect()["core_present"] is False


def test_warn_is_actionable_and_names_both_switch_off_paths(tmp_path, monkeypatch, caplog):
    _home(tmp_path, monkeypatch, config="model:\n  base_url: http://127.0.0.1:8569/v1\n")
    with caplog.at_level(logging.WARNING, logger="compat"):
        result = compat.warn_if_core_present()
    assert result["core_present"] is True
    message = caplog.text
    assert "hermes plugins disable toolrecall-light" in message
    assert "toolrecall stop" in message


def test_warn_is_silent_without_core(tmp_path, monkeypatch, caplog):
    _home(tmp_path, monkeypatch, config="model:\n  name: x\n")
    with caplog.at_level(logging.WARNING, logger="compat"):
        compat.warn_if_core_present()
    assert caplog.text == ""
