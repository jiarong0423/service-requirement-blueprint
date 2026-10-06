from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path

import fontTools
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont


FONT_SPECS = (
    ("NotoSansTC-Regular.ttf", 400, "Regular", "NotoSansTC-Regular", False),
    ("NotoSansTC-Bold.ttf", 700, "Bold", "NotoSansTC-Bold", True),
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def replace_names(font: TTFont, style: str, postscript_name: str) -> None:
    names = font["name"]
    values = {
        1: "Noto Sans TC",
        2: style,
        4: f"Noto Sans TC {style}",
        6: postscript_name,
        16: "Noto Sans TC",
        17: style,
    }
    for name_id, value in values.items():
        names.removeNames(nameID=name_id)
        names.setName(value, name_id, 3, 1, 0x0409)
        names.setName(value, name_id, 0, 4, 0)


def configure_style(font: TTFont, weight: int, is_bold: bool) -> None:
    font["OS/2"].usWeightClass = weight
    if is_bold:
        font["OS/2"].fsSelection |= 1 << 5
        font["OS/2"].fsSelection &= ~(1 << 6)
        font["head"].macStyle |= 1
    else:
        font["OS/2"].fsSelection &= ~(1 << 5)
        font["OS/2"].fsSelection |= 1 << 6
        font["head"].macStyle &= ~1


def build_font(source: Path, destination: Path, weight: int, style: str, postscript_name: str, is_bold: bool) -> None:
    font = TTFont(source, recalcTimestamp=False)
    if "fvar" not in font:
        raise ValueError("source_font_is_not_variable")
    instantiateVariableFont(font, {"wght": weight}, inplace=True)
    configure_style(font, weight, is_bold)
    replace_names(font, style, postscript_name)
    font.save(destination, reorderTables=False)

    check = TTFont(destination)
    if "fvar" in check or check["OS/2"].usWeightClass != weight:
        raise ValueError("font_instance_verification_failed")
    postscript_names = {
        record.toUnicode()
        for record in check["name"].names
        if record.nameID == 6
    }
    if postscript_names != {postscript_name}:
        raise ValueError("font_name_verification_failed")


def write_new(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Build pinned static Noto Sans TC assets for Gate 0.")
    parser.add_argument("--source-font", type=Path, required=True)
    parser.add_argument("--license-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--source-commit", required=True)
    arguments = parser.parse_args()

    if not arguments.source_font.is_file() or not arguments.license_file.is_file():
        raise SystemExit("source_font_or_license_missing")
    arguments.output_dir.mkdir(parents=True, exist_ok=False)

    outputs: dict[str, dict[str, object]] = {}
    try:
        for filename, weight, style, postscript_name, is_bold in FONT_SPECS:
            destination = arguments.output_dir / filename
            build_font(arguments.source_font, destination, weight, style, postscript_name, is_bold)
            outputs[filename] = {
                "bytes": destination.stat().st_size,
                "sha256": sha256(destination),
                "weight": weight,
                "postscript_name": postscript_name,
            }

        license_destination = arguments.output_dir / "OFL.txt"
        write_new(license_destination, arguments.license_file.read_bytes())
        outputs["OFL.txt"] = {
            "bytes": license_destination.stat().st_size,
            "sha256": sha256(license_destination),
        }

        metadata = {
            "schema_version": "gate0-font-source/1.0",
            "family": "Noto Sans TC",
            "license": "SIL Open Font License 1.1",
            "source_commit": arguments.source_commit,
            "source_url": arguments.source_url,
            "source_bytes": arguments.source_font.stat().st_size,
            "source_sha256": sha256(arguments.source_font),
            "fonttools_version": fontTools.__version__,
            "build_command": "python tools/build_gate0_fonts.py --source-font SOURCE.ttf --license-file OFL.txt --output-dir assets/fonts --source-url PINNED_URL --source-commit PINNED_COMMIT",
            "outputs": outputs,
        }
        write_new(
            arguments.output_dir / "SOURCE.json",
            (json.dumps(metadata, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
    except Exception:
        shutil.rmtree(arguments.output_dir, ignore_errors=True)
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
