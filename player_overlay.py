"""把字幕叠加到 PotPlayer / VLC 等桌面播放器上。

原理：创建一个无边框、鼠标穿透、始终置顶的小字幕窗，位置默认跟随
PotPlayer/VLC 窗口；字幕通过本地 SSE 从现有服务读取，不需要 OBS。

用法：
    python player_overlay.py
    python player_overlay.py --no-follow
    python player_overlay.py --bottom 8 --opacity 0.78

退出：关闭启动它的控制台窗口，或在控制台按 Ctrl+C。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import font as tkfont

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config.json"
BOX_DEFAULT = "#080a0e"
TEXT_DEFAULT = "#ffffff"


# ---------- Win32 辅助 ----------

class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class PlayerWindow:
    """按窗口标题查找播放器窗口，返回 (x, y, w, h)。"""

    def __init__(self, keywords: list[str]) -> None:
        self.keywords = [k.lower() for k in keywords if k]
        self._user32 = ctypes.windll.user32 if os.name == "nt" else None
        self._enum_proc = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p
        )(self._callback)
        self._found: list[tuple[int, tuple[int, int, int, int]]] = []

    def _callback(self, hwnd: int, _lparam: int) -> bool:
        u = self._user32
        if u is None:
            return True
        try:
            if not u.IsWindowVisible(hwnd) or u.IsIconic(hwnd):
                return True
            title_len = u.GetWindowTextLengthW(hwnd)
            if title_len <= 0:
                return True
            buf = ctypes.create_unicode_buffer(title_len + 1)
            u.GetWindowTextW(hwnd, buf, title_len + 1)
            title = buf.value.lower()
            if not any(key in title for key in self.keywords):
                return True
            rect = RECT()
            if not u.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            if w < 320 or h < 200:
                return True
            self._found.append((w * h, (rect.left, rect.top, w, h)))
        except Exception:
            pass
        return True

    def find(self) -> tuple[int, int, int, int] | None:
        if self._user32 is None or not self.keywords:
            return None
        self._found = []
        self._user32.EnumWindows(self._enum_proc, 0)
        if not self._found:
            return None
        self._found.sort(key=lambda item: item[0], reverse=True)
        return self._found[0][1]


def set_click_through(root: tk.Tk) -> None:
    """让窗口不接收鼠标、不抢焦点、不显示在任务栏。"""
    if os.name != "nt":
        return
    try:
        u = ctypes.windll.user32
        hwnd = u.GetAncestor(root.winfo_id(), 2)  # GA_ROOT
        if not hwnd:
            return
        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x00080000
        WS_EX_TRANSPARENT = 0x00000020
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_NOACTIVATE = 0x08000000
        style = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
        u.SetWindowLongW(
            hwnd,
            GWL_EXSTYLE,
            style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
        )
    except Exception:
        pass


def set_round_region(root: tk.Tk, width: int, height: int, radius: int) -> None:
    """Clip the native window to a rounded rectangle."""
    if os.name != "nt" or width <= 1 or height <= 1:
        return
    try:
        u = ctypes.windll.user32
        g = ctypes.windll.gdi32
        hwnd = u.GetAncestor(root.winfo_id(), 2)  # GA_ROOT
        if not hwnd:
            return
        r = max(2, radius)
        rgn = g.CreateRoundRectRgn(0, 0, width + 1, height + 1, r * 2, r * 2)
        if not rgn:
            return
        if not u.SetWindowRgn(hwnd, rgn, True):
            g.DeleteObject(rgn)
    except Exception:
        pass


def set_dpi_aware() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def parse_color(value: Any, default: str) -> str:
    if isinstance(value, str):
        s = value.strip()
        if len(s) == 7 and s.startswith("#"):
            try:
                int(s[1:], 16)
                return s
            except ValueError:
                pass
        if s.startswith("rgba(") or s.startswith("rgb("):
            inner = s[s.find("(") + 1 : s.rfind(")")]
            parts = [p.strip() for p in inner.split(",")]
            if len(parts) >= 3:
                try:
                    r, g, b = (max(0, min(255, int(float(p)))) for p in parts[:3])
                    return f"#{r:02x}{g:02x}{b:02x}"
                except ValueError:
                    pass
    return default


# ---------- 字幕窗 ----------

class PlayerOverlay:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("Qwen Caption Overlay")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        if args.opacity < 0.999:
            self.root.attributes("-alpha", max(0.35, min(1.0, args.opacity)))
        self.root.configure(bg=BOX_DEFAULT)
        try:
            self.root.wm_attributes("-toolwindow", True)
        except tk.TclError:
            pass

        self.canvas = tk.Canvas(
            self.root, bg=BOX_DEFAULT, highlightthickness=0, bd=0,
            relief="flat", takefocus=0,
        )
        self.canvas.pack(fill="both", expand=True)

        self.font = tkfont.Font(family="Microsoft YaHei", size=-64, weight="bold")
        self.finals: list[str] = []
        self.partial = ""
        self.style: dict[str, Any] = {
            "font_size": args.font_size or 64,
            "max_lines": args.max_lines or 2,
            "bottom": 6,
            "text_color": TEXT_DEFAULT,
            "box_color": "rgba(0,0,0,0.52)",
            "clear_after": 8,
        }
        self.base_font = int(self.style["font_size"])
        self.max_lines = int(self.style["max_lines"])
        self.clear_after = 8.0
        self.text_color = TEXT_DEFAULT
        self.box_color = BOX_DEFAULT
        self.bottom_pct = 6.0
        self.font_px = 64
        self.line_height = 82
        self.pad_x = 26
        self.pad_top = 10
        self.pad_bottom = 12
        self.corner_radius = 24
        self.window_w = 800
        self.window_h = 180
        self.max_width = 1200
        self.max_height = 200
        self.px, self.py, self.pw, self.ph = 0, 0, 1280, 720
        self._last_activity = 0.0
        self._last_player_rect: tuple[int, int, int, int] | None | object = object()
        self._closed = False

        keywords = [] if args.no_follow else [
            k.strip() for k in args.follow.split(",") if k.strip()
        ]
        self.player = PlayerWindow(keywords)
        self.screen_w = self.root.winfo_screenwidth()
        self.screen_h = self.root.winfo_screenheight()
        self._configure_geometry()
        self.root.deiconify()
        self.root.after(80, lambda: set_click_through(self.root))
        self.root.after(200, self._poll_geometry)
        self.root.after(1000, self._tick_clear)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    # ----- 位置与尺寸 -----

    def _target_rect(self) -> tuple[int, int, int, int]:
        rect = self.player.find()
        if rect is None:
            return (0, 0, self.screen_w, self.screen_h)
        return rect

    def _configure_geometry(self) -> None:
        px, py, pw, ph = self._target_rect()
        pw = max(320, pw)
        ph = max(200, ph)
        self.px, self.py, self.pw, self.ph = px, py, pw, ph
        scale = max(0.55, min(2.0, ph / 1080.0))
        if self.args.font_size:
            self.font_px = max(18, int(self.args.font_size))
        else:
            self.font_px = max(18, round(self.base_font * scale))
        self.font.configure(size=-self.font_px)
        linespace = int(self.font.metrics("linespace"))
        self.line_height = max(linespace, round(self.font_px * 1.28))
        self.pad_x = max(10, round(26 * scale))
        self.pad_top = max(6, round(10 * scale))
        self.pad_bottom = max(7, round(12 * scale))
        self.corner_radius = max(10, round(18 * scale))
        self.max_width = max(320, min(round(pw * 0.92), round(self.screen_w * 0.94)))
        self.max_height = self.max_lines * self.line_height + self.pad_top + self.pad_bottom
        self.canvas.configure(width=self.max_width, height=self.max_height)
        self.root.geometry(f"{self.max_width}x{self.max_height}+0+0")
        self.render()

    def _position(self, w: int, h: int) -> None:
        x = self.px + (self.pw - w) // 2
        y = self.py + self.ph - round(self.ph * self.bottom_pct / 100.0) - h
        y = max(0, y)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _poll_geometry(self) -> None:
        if self._closed:
            return
        try:
            rect = self._target_rect()
            if rect != self._last_player_rect:
                self._last_player_rect = rect
                self._configure_geometry()
            if self.partial or self.finals:
                self.root.lift()
        except Exception:
            pass
        self.root.after(600, self._poll_geometry)

    # ----- 字幕 -----

    def apply_style(self, style: dict[str, Any] | None) -> None:
        if not style:
            return
        self.style.update(style)
        try:
            self.base_font = max(18, min(160, int(style.get("font_size", self.base_font))))
        except (TypeError, ValueError):
            pass
        try:
            self.max_lines = max(1, min(4, int(style.get("max_lines", self.max_lines))))
        except (TypeError, ValueError):
            pass
        try:
            self.bottom_pct = max(0.0, min(40.0, float(style.get("bottom", self.bottom_pct))))
        except (TypeError, ValueError):
            pass
        try:
            self.clear_after = max(0.0, float(style.get("clear_after", self.clear_after)))
        except (TypeError, ValueError):
            pass
        self.text_color = parse_color(style.get("text_color"), TEXT_DEFAULT)
        self.box_color = parse_color(style.get("box_color"), BOX_DEFAULT)
        self.root.configure(bg=self.box_color)
        self.canvas.configure(bg=self.box_color)
        self._configure_geometry()

    def set_caption(self, finals: Any, partial: Any) -> None:
        self.finals = [str(x) for x in finals] if isinstance(finals, list) else []
        self.partial = str(partial or "")
        self._last_activity = time.monotonic()
        self.render()

    def _caption_text(self) -> str:
        if self.partial:
            keep = max(0, self.max_lines - 1)
            items = self.finals[-keep:] if keep > 0 else []
            items = items + [self.partial]
        else:
            items = self.finals[-self.max_lines:]
        return "\n".join(item for item in items if item)

    def _draw(self, text: str, w: int, h: int) -> tuple[int, int, int, int] | None:
        wrap = max(80, w - self.pad_x * 2)
        y = h - self.pad_bottom
        self.canvas.delete("all")
        common = dict(
            text=text, width=wrap, justify="center", anchor="s",
            font=self.font,
        )
        shadow = self.canvas.create_text(
            w // 2 + max(1, self.font_px // 40),
            y + max(1, self.font_px // 32),
            fill="#000000", **common,
        )
        main = self.canvas.create_text(w // 2, y, fill=self.text_color, **common)
        bbox = self.canvas.bbox(main)
        if bbox:
            # 给文字一点内边距，避免贴边。
            self.canvas.tag_raise(shadow)
            self.canvas.tag_raise(main)
        return bbox

    def render(self) -> None:
        text = self._caption_text()
        if not text:
            self.canvas.delete("all")
            self.root.withdraw()
            return
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self.root.configure(bg=self.box_color)
        self.canvas.configure(bg=self.box_color, width=self.max_width, height=self.max_height)
        # Use the full available width so wrapping stays stable.
        bbox = self._draw(text, self.max_width, self.max_height)
        if not bbox:
            self.root.withdraw()
            return
        w, h = self.max_width, self.max_height
        self.window_w, self.window_h = w, h
        self.canvas.configure(width=w, height=h)
        self._position(w, h)
        self._draw(text, w, h)
        self.root.update_idletasks()
        set_round_region(self.root, w, h, self.corner_radius)
        self.root.lift()

    def _tick_clear(self) -> None:
        if self._closed:
            return
        if (self.clear_after > 0 and (self.finals or self.partial)
                and time.monotonic() - self._last_activity > self.clear_after):
            self.finals = []
            self.partial = ""
            self.render()
        self.root.after(1000, self._tick_clear)

    def close(self) -> None:
        self._closed = True
        try:
            self.root.destroy()
        except tk.TclError:
            pass


# ---------- SSE ----------

class SSEClient(threading.Thread):
    def __init__(self, url: str, on_event: Any, root: tk.Tk) -> None:
        super().__init__(daemon=True, name="caption-sse")
        self.url = url
        self.on_event = on_event
        self.root = root
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                req = urllib.request.Request(
                    self.url,
                    headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"},
                )
                with urllib.request.urlopen(req, timeout=35) as resp:
                    for raw in resp:
                        if self.stop_event.is_set():
                            return
                        line = raw.decode("utf-8", "replace").rstrip("\r\n")
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].lstrip()
                        try:
                            event = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        try:
                            self.root.after(0, self._dispatch, event)
                        except tk.TclError:
                            return
            except Exception:
                if self.stop_event.is_set():
                    return
                time.sleep(2.0)

    def _dispatch(self, event: dict[str, Any]) -> None:
        try:
            self.on_event(event)
        except Exception:
            import traceback
            traceback.print_exc()


# ---------- 入口 ----------

def load_defaults(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def main() -> int:
    if os.name != "nt":
        print("这个桌面字幕窗目前只支持 Windows。", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description="在 PotPlayer/VLC 上叠加实时字幕")
    parser.add_argument("--server", default=None, help="字幕服务地址，默认按 config.json")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="读取端口和样式的配置文件")
    parser.add_argument("--font-size", type=int, default=None, help="固定字号（像素），默认按画面高度缩放")
    parser.add_argument("--max-lines", type=int, default=None, help="最多显示行数")
    parser.add_argument("--bottom", type=float, default=None, help="距底部百分比")
    parser.add_argument("--opacity", type=float, default=0.78, help="整体不透明度 0.35-1.0")
    parser.add_argument("--follow", default="PotPlayer,VLC", help="跟随的窗口标题关键词，逗号分隔")
    parser.add_argument("--no-follow", action="store_true", help="不跟随播放器，固定屏幕底部")
    args = parser.parse_args()
    defaults = load_defaults(Path(args.config))

    if not args.server:
        host = str(defaults.get("server_host") or "127.0.0.1")
        port = defaults.get("server_port") or 8765
        args.server = f"http://{host}:{port}"
    args.server = args.server.rstrip("/")

    set_dpi_aware()
    overlay = PlayerOverlay(args)
    overlay.apply_style({
        "font_size": defaults.get("font_size", 64),
        "max_lines": args.max_lines or defaults.get("max_lines", 2),
        "bottom": args.bottom if args.bottom is not None else defaults.get("bottom", 6),
        "text_color": defaults.get("text_color", TEXT_DEFAULT),
        "box_color": defaults.get("box_color", "rgba(0,0,0,0.52)"),
        "clear_after": defaults.get("clear_after", 8),
    })

    def handle(event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "snapshot":
            if isinstance(event.get("style"), dict):
                overlay.apply_style(event["style"])
            caption = event.get("caption") or {}
            if isinstance(caption, dict):
                overlay.set_caption(caption.get("finals"), caption.get("partial"))
        elif kind == "caption":
            overlay.set_caption(event.get("finals"), event.get("partial"))
        elif kind == "style":
            overlay.apply_style(event.get("style"))
        elif kind == "clear":
            overlay.set_caption([], "")

    client = SSEClient(args.server + "/api/events", handle, overlay.root)
    client.start()
    print(f"桌面字幕窗已启动，服务：{args.server}")
    print("关闭此控制台或按 Ctrl+C 退出。")
    try:
        overlay.root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        client.stop_event.set()
        overlay.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
