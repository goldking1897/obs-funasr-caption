"""常用词/热词配置的纯函数测试。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import DEFAULT_CONFIG, ConfigError, build_vocabulary, validate_patch


def main() -> int:
    vocab = build_vocabulary(["阿里云", "FunASR=5", "超级词=50", "错误=9"])
    assert vocab == {"阿里云": 5, "FunASR": 5, "超级词": 50, "错误": 5}, vocab

    cleaned = validate_patch(
        {"hotwords": [" 阿里云 ", "FunASR=3", "", "超级词=50"]},
        dict(DEFAULT_CONFIG),
    )["hotwords"]
    assert cleaned == ["阿里云", "FunASR=3", "超级词=50"], cleaned

    for bad in (["x=0"], ["x=6"], ["x=abc"], ["=5"]):
        try:
            validate_patch({"hotwords": bad}, dict(DEFAULT_CONFIG))
        except ConfigError:
            continue
        raise AssertionError(f"应当拒绝：{bad}")

    items = [f"w{i}" for i in range(2001)]
    try:
        validate_patch({"hotwords": items}, dict(DEFAULT_CONFIG))
    except ConfigError:
        pass
    else:
        raise AssertionError("超过 2000 个热词没有被拒绝")

    print("常用词解析、权重与数量限制均通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
