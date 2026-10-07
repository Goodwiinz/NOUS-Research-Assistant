"""Every ffmpeg/ffprobe invocation must carry -protocol_whitelist (GOO-409 / F2).

The services shell out to binaries absent from CI, so this pins the invariant
by scanning the source of every call site.
"""

from __future__ import annotations

import re
from pathlib import Path

PROCESSING = Path(__file__).resolve().parents[4] / "src" / "services" / "processing"


def _argv_lists(text: str, binary: str) -> list[str]:
    # Each list literal that starts with the binary name, up to the closing ']'.
    return re.findall(rf'\[\s*"{binary}",(.*?)\]', text, flags=re.S)


def test_subprocess_argv_lists_whitelist_protocols_first() -> None:
    expected = re.compile(r'^\s*"-protocol_whitelist",\s*"file,pipe",')
    found = 0
    for name in ("video_processing_service.py", "audio_processing_service.py"):
        text = (PROCESSING / name).read_text()
        argvs = _argv_lists(text, "ffprobe") + _argv_lists(text, "ffmpeg")
        assert argvs, f"no ffmpeg/ffprobe argv found in {name}"
        for argv in argvs:
            found += 1
            assert expected.match(
                argv
            ), f"{name}: argv must start with -protocol_whitelist:\n{argv}"
    # video: ffprobe + 2 ffmpeg; audio: ffprobe. A new call site must be added here.
    assert found == 4


def test_ffmpeg_python_input_sets_protocol_whitelist() -> None:
    text = (PROCESSING / "multimodal_processing_service.py").read_text()
    calls = re.findall(r"ffmpeg\.input\(([^)]*)\)", text)
    assert calls, "no ffmpeg.input call found"
    for args in calls:
        assert 'protocol_whitelist="file,pipe"' in args, args
