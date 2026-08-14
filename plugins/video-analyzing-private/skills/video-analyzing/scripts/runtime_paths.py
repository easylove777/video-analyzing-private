from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Mapping


def resolve_data_root(
    explicit: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    env = os.environ if environ is None else environ
    if explicit:
        return Path(explicit).expanduser().resolve()
    if env.get("VIDEO_ANALYZING_DATA_ROOT"):
        return Path(env["VIDEO_ANALYZING_DATA_ROOT"]).expanduser().resolve()
    if env.get("PLUGIN_DATA"):
        return (Path(env["PLUGIN_DATA"]) / "video-analyzing-data").resolve()
    if env.get("LOCALAPPDATA"):
        return (Path(env["LOCALAPPDATA"]) / "video-analyzing" / "data").resolve()
    return (Path.home() / ".local" / "share" / "video-analyzing" / "data").resolve()


def add_data_root_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-root")


def resolved_data_root(args: argparse.Namespace) -> Path:
    return resolve_data_root(getattr(args, "data_root", None))
