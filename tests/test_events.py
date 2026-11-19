from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from events import CaptionState


def main() -> int:
    state = CaptionState()

    # 字符串 sentence_id 不能被当成同一个句子而丢掉。
    event = state.apply_sentence(
        {"text": "第一句。", "sentence_id": "s-1", "sentence_end": True}
    )
    assert event is not None
    event = state.apply_sentence(
        {"text": "第二句。", "sentence_id": "s-2", "sentence_end": True}
    )
    assert event is not None
    assert event["finals"] == ["第一句。", "第二句。"], event

    # 同一条最终结果重复到达时去重。
    event = state.apply_sentence(
        {"text": "第二句。", "sentence_id": "s-2", "sentence_end": True}
    )
    assert event is not None
    assert event["finals"] == ["第一句。", "第二句。"], event

    # heartbeat 不产生字幕。
    assert state.apply_sentence({"heartbeat": True}) is None

    # 空中间结果不覆盖已有中间结果。
    state.apply_sentence({"text": "正在说", "sentence_id": "s-3", "sentence_end": False})
    state.apply_sentence({"text": "", "sentence_id": "s-3", "sentence_end": False})
    assert state.snapshot()["partial"] == "正在说"

    print("OK: 字幕状态去重与中间结果")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
