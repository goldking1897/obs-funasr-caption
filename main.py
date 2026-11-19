"""启动入口：python main.py"""
from __future__ import annotations

import argparse
import logging
import sys
import time
import webbrowser
from pathlib import Path

from app import App
from config import DEFAULT_CONFIG_PATH
from server import serve


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OBS 阿里云 Qwen-Audio-ASR-Streaming 实时字幕")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="配置文件路径")
    parser.add_argument("--host", default=None, help="监听地址（默认读取配置，通常 127.0.0.1）")
    parser.add_argument("--port", type=int, default=None, help="监听端口（默认读取配置，通常 8765）")
    parser.add_argument("--demo", action="store_true", help="强制使用演示字幕，不连接云端")
    parser.add_argument("--open", action="store_true", help="启动后自动打开控制台")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(name)-6s %(message)s",
        datefmt="%H:%M:%S",
    )
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    app = App(args.config)
    if args.demo:
        app.config["demo"] = True

    host = args.host or str(app.config.get("server_host") or "127.0.0.1")
    port = args.port or int(app.config.get("server_port") or 8765)

    try:
        server = serve(app, host, port)
    except OSError as exc:
        print(f"\n启动失败：端口 {host}:{port} 无法监听（{exc}）。", file=sys.stderr)
        print("可能已有一个实例在运行；或者用 --port 8766 换一个端口。\n", file=sys.stderr)
        return 1

    control_url = f"http://{host}:{port}/control"
    overlay_url = f"http://{host}:{port}/overlay"
    print()
    print("=" * 62)
    print("  OBS 阿里云 Qwen-Audio-ASR 实时字幕已启动")
    print(f"  控制台：      {control_url}")
    print(f"  OBS 浏览器源：{overlay_url}")
    print("  在 OBS 中新建“浏览器源”，粘贴上面的浏览器源地址，")
    print("  建议宽度 1920、高度 1080（与 1080p 画布一致），背景透明。")
    print("  按 Ctrl+C 停止。")
    print("=" * 62)
    print()

    try:
        app.start()
        if args.open:
            webbrowser.open(control_url)
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        app.stop()
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
