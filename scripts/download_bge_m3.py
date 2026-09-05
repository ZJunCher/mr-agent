#!/usr/bin/env python3
"""Download one immutable BGE-M3 snapshot into a shared Hugging Face cache."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

from ut_agent.repair_memory.embedding import BGE_MODEL_NAME, BGE_MODEL_REVISION

_MARKER_NAME = ".bge-m3-download.json"


def _immutable_revision(revision: str) -> str:
    value = str(revision or "").strip()
    if not value or value.casefold() == "main":
        raise ValueError("model revision must be an immutable commit")
    return value


def _completed_snapshot(target: Path, *, model: str, revision: str) -> Path | None:
    marker = target / _MARKER_NAME
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        snapshot = Path(str(payload["snapshot_path"])).resolve()
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if payload.get("model") != model or payload.get("revision") != revision:
        return None
    try:
        snapshot.relative_to(target)
    except ValueError:
        return None
    if not snapshot.is_dir() or not (snapshot / "config.json").is_file():
        return None
    return snapshot


def _write_marker(target: Path, *, model: str, revision: str, snapshot: Path) -> None:
    marker = target / _MARKER_NAME
    temporary = target / f"{_MARKER_NAME}.tmp"
    temporary.write_text(
        json.dumps(
            {"model": model, "revision": revision, "snapshot_path": str(snapshot)},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    temporary.replace(marker)


def _print_result(output: TextIO, *, target: Path, revision: str, reused: bool) -> None:
    print(f"model={BGE_MODEL_NAME}", file=output)
    print(f"revision={revision}", file=output)
    print(f"target={target}", file=output)
    print(f"download_reused={str(reused).lower()}", file=output)
    print("download_complete=true", file=output)


def download_model(
    target: str | Path,
    *,
    revision: str = BGE_MODEL_REVISION,
    snapshot_download_fn: Callable[..., str] | None = None,
    output: TextIO = sys.stdout,
) -> Path:
    """Download or reuse one complete immutable snapshot without logging credentials."""
    validated_revision = _immutable_revision(revision)
    target_path = Path(target).expanduser().resolve()
    target_path.mkdir(parents=True, exist_ok=True)
    completed = _completed_snapshot(target_path, model=BGE_MODEL_NAME, revision=validated_revision)
    if completed is not None:
        _print_result(output, target=target_path, revision=validated_revision, reused=True)
        return completed

    if snapshot_download_fn is None:
        from huggingface_hub import snapshot_download

        snapshot_download_fn = snapshot_download
    downloaded = Path(
        snapshot_download_fn(
            repo_id=BGE_MODEL_NAME,
            revision=validated_revision,
            cache_dir=str(target_path),
            ignore_patterns=("onnx/*",),
        )
    ).resolve()
    try:
        downloaded.relative_to(target_path)
    except ValueError as error:
        raise RuntimeError("downloaded snapshot is outside the target directory") from error
    if not downloaded.is_dir() or not (downloaded / "config.json").is_file():
        raise RuntimeError("downloaded snapshot is incomplete")
    _write_marker(target_path, model=BGE_MODEL_NAME, revision=validated_revision, snapshot=downloaded)
    _print_result(output, target=target_path, revision=validated_revision, reused=False)
    return downloaded


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download the fixed BGE-M3 model snapshot")
    parser.add_argument("--target", required=True, help="shared Hugging Face cache directory")
    parser.add_argument("--revision", default=BGE_MODEL_REVISION, help="immutable model commit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        download_model(args.target, revision=args.revision)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"download_failed={type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
