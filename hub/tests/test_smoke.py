"""DT-1 smoke test: the package imports and the skeleton entry points work."""
from __future__ import annotations

import importlib
import pkgutil

from fastapi import FastAPI

import daytrace_hub
from daytrace_hub import __version__
from daytrace_hub.__main__ import main
from daytrace_hub.app import create_app


def test_version() -> None:
    assert __version__ == "0.1.0"


def test_create_app() -> None:
    assert isinstance(create_app(), FastAPI)


def test_cli_without_command_prints_help(capsys) -> None:
    assert main([]) == 0
    assert "daytrace-hub" in capsys.readouterr().out


def test_every_module_imports() -> None:
    for module in pkgutil.walk_packages(daytrace_hub.__path__, prefix="daytrace_hub."):
        importlib.import_module(module.name)
