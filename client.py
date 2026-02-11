#!/usr/bin/env python3
"""
Terminal Chat Tkinter client.

Runs on both machines:
- outbound-only connection to server
- cinematic terminal-style rendering
- optional director controls in the same UI
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
import queue
import random
import socket
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional


class NetworkClient:
    def __init__(self, host: str, port: int, name: str) -> None:
        self.host = host
        self.port = port
        self.name = name
        self.sock: Optional[socket.socket] = None
        self.running = threading.Event()
        self.incoming: "queue.Queue[dict]" = queue.Queue()
        self.connected = threading.Event()
        self.last_status: Optional[str] = None

    def start(self) -> None:
        self.running.set()
        threading.Thread(target=self._connect_loop, daemon=True).start()

    def stop(self) -> None:
        self.running.clear()
        self.connected.clear()
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass

    def send(self, event: dict) -> bool:
        if not self.connected.is_set() or self.sock is None:
            return False
        payload = json.dumps(event, ensure_ascii=True) + "\n"
        try:
            self.sock.sendall(payload.encode("utf-8"))
            return True
        except OSError:
            self.connected.clear()
            return False

    def _connect_loop(self) -> None:
        while self.running.is_set():
            if not self.connected.is_set():
                self._try_connect()
            time.sleep(1.0)

    def _try_connect(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        try:
            sock.connect((self.host, self.port))
            hello = json.dumps({"type": "hello", "name": self.name}) + "\n"
            sock.sendall(hello.encode("utf-8"))
            sock.settimeout(None)
            self.sock = sock
            self.connected.set()
            self._push_status("Connected to server.")
            threading.Thread(target=self._reader_loop, daemon=True).start()
        except OSError:
            try:
                sock.close()
            except OSError:
                pass
            self._push_status("Reconnecting...")

    def _reader_loop(self) -> None:
        assert self.sock is not None
        file = self.sock.makefile("r", encoding="utf-8", newline="\n")
        while self.running.is_set():
            try:
                line = file.readline()
            except OSError:
                break
            if not line:
                break
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.incoming.put(event)
        self.connected.clear()
        self._push_status("Disconnected from server.")
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _push_status(self, text: str) -> None:
        if self.last_status == text:
            return
        self.last_status = text
        self.incoming.put({"type": "system", "text": text})


class TerminalUI:
    def __init__(self, root: tk.Tk, net: NetworkClient, name: str, director: bool = False) -> None:
        self.root = root
        self.net = net
        self.name = name
        self.director = director
        self.typing_flags: dict[str, bool] = {}
        self.crashed = False
        self.crash_timer_job: Optional[str] = None
        self.crash_anim_job: Optional[str] = None
        self.crash_seconds_left = 0
        self.portal_active = False
        self.portal_anim_job: Optional[str] = None
        self.portal_close_job: Optional[str] = None
        self.portal_phase = 0.0
        self.sequence_jobs: list[str] = []

        self.root.title("Terminal Chat")
        self.root.geometry("960x620")
        self.root.configure(bg="#111315")

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Main.TFrame", background="#111315")
        style.configure(
            "Input.TEntry",
            fieldbackground="#0a0d10",
            foreground="#d5f6de",
            borderwidth=1,
            insertcolor="#d5f6de",
        )
        style.configure("TButton", padding=6)

        frame = ttk.Frame(root, style="Main.TFrame", padding=8)
        frame.pack(fill="both", expand=True)
        self.main_frame = frame

        self._build_chrome(frame)
        self._build_terminal(frame)
        self._build_input(frame)
        if self.director:
            self._build_director_panel(frame)
        self._build_crash_overlay(frame)
        self._build_portal_overlay(frame)

        self.root.bind("<Return>", self._on_enter)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._pump_incoming()

    def _build_chrome(self, parent: ttk.Frame) -> None:
        bar = tk.Frame(parent, bg="#191d22", height=30)
        bar.pack(fill="x", pady=(0, 8))
        tk.Frame(bar, bg="#ff5f56", width=12, height=12).pack(side="left", padx=(8, 4), pady=9)
        tk.Frame(bar, bg="#ffbd2e", width=12, height=12).pack(side="left", padx=4, pady=9)
        tk.Frame(bar, bg="#27c93f", width=12, height=12).pack(side="left", padx=4, pady=9)
        label = tk.Label(
            bar, text="secure-terminal :: session", fg="#9fc1cc", bg="#191d22", font=("Consolas", 11)
        )
        label.pack(side="left", padx=10)

    def _build_terminal(self, parent: ttk.Frame) -> None:
        body = tk.Frame(parent, bg="#0a0d10", bd=1, relief="solid")
        body.pack(fill="both", expand=True)

        self.text = tk.Text(
            body,
            bg="#0a0d10",
            fg="#d5f6de",
            insertbackground="#d5f6de",
            font=("Consolas", 12),
            wrap="word",
            bd=0,
            padx=12,
            pady=12,
            state="disabled",
        )
        self.text.pack(fill="both", expand=True)
        self.text.tag_configure("chat", foreground="#d5f6de")
        self.text.tag_configure("system", foreground="#f7c266")
        self.text.tag_configure("glitch", foreground="#ff6b6b")
        self.text.tag_configure("meta", foreground="#7ea0ab")

        self.typing_label = tk.Label(
            parent, text="", fg="#7ea0ab", bg="#111315", font=("Consolas", 10), anchor="w"
        )
        self.typing_label.pack(fill="x", pady=(6, 0))

    def _build_input(self, parent: ttk.Frame) -> None:
        row = ttk.Frame(parent, style="Main.TFrame")
        row.pack(fill="x", pady=(8, 0))
        self.entry = ttk.Entry(row, style="Input.TEntry")
        self.entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        send_btn = ttk.Button(row, text="Send", command=self._send_message)
        send_btn.pack(side="right")
        self.entry.focus_set()

    def _build_director_panel(self, parent: ttk.Frame) -> None:
        panel = ttk.Frame(parent, style="Main.TFrame")
        panel.pack(fill="x", pady=(8, 0))
        ttk.Button(panel, text="System Alert", command=lambda: self._inject("system")).pack(side="left", padx=4)
        ttk.Button(panel, text="Glitch Burst", command=lambda: self._inject("glitch")).pack(side="left", padx=4)
        ttk.Button(panel, text="Remote Typing", command=self._toggle_remote_typing).pack(side="left", padx=4)
        ttk.Button(panel, text="Demo Sequence", command=self._run_demo_sequence).pack(side="left", padx=4)
        self.remote_typing = False

    def _build_crash_overlay(self, parent: ttk.Frame) -> None:
        self.crash_overlay = tk.Frame(parent, bg="#050608")
        self.crash_title = tk.Label(
            self.crash_overlay,
            text="PROCESS TERMINATED",
            fg="#ff5050",
            bg="#050608",
            font=("Consolas", 26, "bold"),
        )
        self.crash_title.pack(pady=(140, 12))
        self.crash_detail = tk.Label(
            self.crash_overlay,
            text="",
            fg="#f7c266",
            bg="#050608",
            font=("Consolas", 13),
        )
        self.crash_detail.pack(pady=4)
        self.crash_noise = tk.Label(
            self.crash_overlay,
            text="",
            fg="#ff6b6b",
            bg="#050608",
            font=("Consolas", 11),
        )
        self.crash_noise.pack(pady=6)
        self.crash_hint = tk.Label(
            self.crash_overlay,
            text="",
            fg="#9fb9c2",
            bg="#050608",
            font=("Consolas", 11),
        )
        self.crash_hint.pack(pady=4)

    def _build_portal_overlay(self, parent: ttk.Frame) -> None:
        self.portal_overlay = tk.Frame(parent, bg="#050608")
        self.portal_canvas = tk.Canvas(
            self.portal_overlay, bg="#050608", highlightthickness=0, bd=0
        )
        self.portal_canvas.pack(fill="both", expand=True)
        self.portal_text = self.portal_canvas.create_text(
            0,
            0,
            text="",
            fill="#f7c266",
            font=("Consolas", 16, "bold"),
            anchor="center",
        )

    def _inject(self, kind: str) -> None:
        if kind == "system":
            self.net.send({"type": "system", "text": "SECURITY TRACE FLAGGED", "time": self._ts()})
        elif kind == "glitch":
            text = f"JITTER {random.randint(1000, 9999)} :: {random.choice(['##', '@@', '!!']) * 5}"
            self.net.send({"type": "glitch", "text": text, "time": self._ts()})

    def _toggle_remote_typing(self) -> None:
        self.remote_typing = not self.remote_typing
        self.net.send({"type": "typing", "from": "REMOTE", "state": self.remote_typing, "time": self._ts()})

    def _run_demo_sequence(self) -> None:
        def worker() -> None:
            self.net.send(
                {"type": "sequence", "action": "director_protocol", "time": self._ts()}
            )

        threading.Thread(target=worker, daemon=True).start()

    def _on_enter(self, _: tk.Event) -> None:
        self._send_message()

    def _send_message(self) -> None:
        if self.crashed:
            return
        text = self.entry.get().strip()
        if not text:
            return
        event = {"type": "chat", "from": self.name, "text": text, "time": self._ts()}
        ok = self.net.send(event)
        if not ok:
            self._append_line("[offline] message not sent", "system")
            return
        self.entry.delete(0, "end")

    def _append_line(self, text: str, tag: str) -> None:
        self.text.configure(state="normal")
        self.text.insert("end", text + "\n", tag)
        self.text.configure(state="disabled")
        self.text.see("end")

    def _render_event(self, event: dict) -> None:
        kind = event.get("type", "chat")
        ts = event.get("time", self._ts())
        if kind == "clear":
            self._clear_terminal()
            return
        if kind == "crash":
            action = str(event.get("action", "start")).lower()
            if action == "start":
                seconds = int(event.get("seconds", 6))
                reason = str(event.get("reason", "FATAL ERROR"))
                self._start_fake_crash(seconds, reason)
            elif action == "recover":
                self._stop_fake_crash(str(event.get("text", "Session recovered.")))
            return
        if kind == "portal":
            action = str(event.get("action", "open")).lower()
            if action == "open":
                seconds = int(event.get("seconds", 6))
                text = str(
                    event.get(
                        "text", "Let's see how well you survive your own creation."
                    )
                )
                self._start_portal_effect(text, seconds)
            elif action == "close":
                self._stop_portal_effect()
            return
        if kind == "sequence":
            action = str(event.get("action", "director_protocol")).lower()
            if action == "director_protocol":
                self._start_director_protocol_sequence()
            return
        if kind == "typing":
            who = str(event.get("from", "REMOTE"))
            self.typing_flags[who] = bool(event.get("state", False))
            active = [name for name, state in self.typing_flags.items() if state]
            self.typing_label.configure(text=(f"{', '.join(active)} typing..." if active else ""))
            return
        if kind == "chat":
            sender = event.get("from", "UNKNOWN")
            self._append_with_effect(f"[{ts}] {sender}> {event.get('text', '')}", "chat")
            return
        if kind == "glitch":
            self._append_with_effect(f"[{ts}] !! {event.get('text', '')}", "glitch", jitter=True)
            return
        if kind == "system":
            self._append_with_effect(f"[{ts}] [SYS] {event.get('text', '')}", "system")
            return
        self._append_line(f"[{ts}] {event}", "meta")

    def _append_with_effect(self, text: str, tag: str, jitter: bool = False) -> None:
        self.text.configure(state="normal")
        if not jitter:
            self.text.insert("end", text + "\n", tag)
        else:
            scrambled = list(text)
            for i in range(min(4, len(scrambled))):
                idx = random.randint(0, len(scrambled) - 1)
                scrambled[idx] = random.choice("#@$%&*")
                self.text.insert("end", "".join(scrambled) + "\n", tag)
            self.text.insert("end", text + "\n", tag)
        self.text.configure(state="disabled")
        self.text.see("end")

    def _pump_incoming(self) -> None:
        while True:
            try:
                event = self.net.incoming.get_nowait()
            except queue.Empty:
                break
            self._render_event(event)
        self.root.after(50, self._pump_incoming)

    def _clear_terminal(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def _cancel_sequence_jobs(self) -> None:
        for job in self.sequence_jobs:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
        self.sequence_jobs.clear()

    def _start_director_protocol_sequence(self) -> None:
        self._cancel_sequence_jobs()
        logo_lines = [
            "      ███████████",
            "   ███░░░░░░░░░░░███",
            "  ██░░███░░░███░░░░██",
            " ██░░███░░░░░███░░░░██",
            "██░░███░░░██░░███░░░░██",
            "██░░██░░█████░░██░░░░██",
            "██░░██░░█████░░██░░░░██",
            "██░░███░░░██░░███░░░░██",
            " ██░░███░░░░░███░░░░██",
            "  ██░░███░░███░░░░██",
            "   ███░░░░░░░░░░███",
            "      ███████████",
        ]
        glitch_pool = [
            "010101110110",
            "{#@$%}{#@$%}",
            "SEGFAULT::0x4F2A9C",
            "def _shadow_hook(...):",
            "NULL PTR >>> STACK TRACE",
            "RX_OVERRUN // BUS_NOISE",
            "for i in range(9999): panic()",
        ]
        blink_frames = ["> _", ">  ", "> _", ">  ", "> _"]
        actions: list[tuple[int, Callable[[], None]]] = [
            (0, lambda: self._append_line("> INITIALIZING DIRECTOR.PROTOCOL", "system")),
        ]
        actions.append((500, lambda: None))
        for _ in range(random.randint(7, 12)):
            line = random.choice(glitch_pool)
            actions.append((random.randint(50, 80), lambda text=line: self._append_line(text, "glitch")))
        actions.append((200, self._clear_terminal))
        for line in logo_lines:
            actions.append((random.randint(60, 120), lambda text=line: self._append_line(text, "chat")))
        actions.append((200, lambda: self._append_line("> IDENTITY: DIRECTOR", "system")))
        actions.append((150, lambda: self._append_line("> STATUS: OBSERVING", "system")))
        for frame in blink_frames:
            actions.append((130, lambda text=frame: self._append_line(text, "meta")))
        actions.append((280, lambda: self._append_line("I see you.", "chat")))

        def run_step(index: int) -> None:
            if index >= len(actions):
                return
            delay_ms, fn = actions[index]

            def invoke() -> None:
                fn()
                run_step(index + 1)

            job = self.root.after(delay_ms, invoke)
            self.sequence_jobs.append(job)

        run_step(0)

    def _start_fake_crash(self, seconds: int, reason: str) -> None:
        if self.crash_timer_job:
            self.root.after_cancel(self.crash_timer_job)
            self.crash_timer_job = None
        if self.crash_anim_job:
            self.root.after_cancel(self.crash_anim_job)
            self.crash_anim_job = None
        self.crashed = True
        self.entry.configure(state="disabled")
        self.crash_seconds_left = max(2, min(20, seconds))
        self.crash_detail.configure(text=reason)
        self.crash_overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._animate_crash_noise()
        self._tick_crash_timer()

    def _stop_fake_crash(self, note: str) -> None:
        if self.crash_timer_job:
            self.root.after_cancel(self.crash_timer_job)
            self.crash_timer_job = None
        if self.crash_anim_job:
            self.root.after_cancel(self.crash_anim_job)
            self.crash_anim_job = None
        self.crashed = False
        self.crash_overlay.place_forget()
        if not self.portal_active:
            self.entry.configure(state="normal")
            self.entry.focus_set()
        self._append_with_effect(f"[{self._ts()}] [SYS] {note}", "system")

    def _tick_crash_timer(self) -> None:
        if not self.crashed:
            return
        self.crash_hint.configure(
            text=f"Attempting auto-recovery in {self.crash_seconds_left}s ..."
        )
        self.crash_seconds_left -= 1
        if self.crash_seconds_left < 0:
            self._stop_fake_crash("Session restored after crash.")
            return
        self.crash_timer_job = self.root.after(1000, self._tick_crash_timer)

    def _animate_crash_noise(self) -> None:
        if not self.crashed:
            return
        chunks = []
        for _ in range(3):
            chunks.append(
                "".join(random.choice("0123456789ABCDEF#@$%") for _ in range(14))
            )
        self.crash_noise.configure(text=" :: ".join(chunks))
        self.crash_anim_job = self.root.after(90, self._animate_crash_noise)

    def _start_portal_effect(self, text: str, seconds: int) -> None:
        if self.crashed:
            return
        if self.portal_close_job:
            self.root.after_cancel(self.portal_close_job)
            self.portal_close_job = None
        if self.portal_anim_job:
            self.root.after_cancel(self.portal_anim_job)
            self.portal_anim_job = None
        self.portal_active = True
        self.portal_phase = 0.0
        self.entry.configure(state="disabled")
        self.portal_overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.portal_canvas.update_idletasks()
        w = self.portal_canvas.winfo_width()
        h = self.portal_canvas.winfo_height()
        self.portal_canvas.coords(self.portal_text, w / 2, h * 0.82)
        self.portal_canvas.itemconfigure(self.portal_text, text=text)
        self._animate_portal()
        hold_ms = max(2000, min(15000, seconds * 1000))
        self.portal_close_job = self.root.after(hold_ms, self._stop_portal_effect)

    def _animate_portal(self) -> None:
        if not self.portal_active:
            return
        self.portal_canvas.delete("portal_ring")
        w = max(10, self.portal_canvas.winfo_width())
        h = max(10, self.portal_canvas.winfo_height())
        cx = w / 2
        cy = h / 2
        self.portal_phase += 0.23
        pulse = (1 + (0.5 * (1 + math.sin(self.portal_phase)))) * 0.28
        base_r = min(w, h) * pulse
        for i in range(8):
            rr = base_r + i * 18
            shade = 255 - i * 22
            color = f"#{0:02x}{max(40, shade):02x}{max(70, shade):02x}"
            self.portal_canvas.create_oval(
                cx - rr,
                cy - rr,
                cx + rr,
                cy + rr,
                outline=color,
                width=2,
                tags="portal_ring",
            )
        self.portal_canvas.create_text(
            cx,
            cy,
            text="PORTAL ONLINE",
            fill="#d5f6de",
            font=("Consolas", 22, "bold"),
            tags="portal_ring",
        )
        self.portal_anim_job = self.root.after(45, self._animate_portal)

    def _stop_portal_effect(self) -> None:
        if self.portal_close_job:
            self.root.after_cancel(self.portal_close_job)
            self.portal_close_job = None
        if self.portal_anim_job:
            self.root.after_cancel(self.portal_anim_job)
            self.portal_anim_job = None
        self.portal_active = False
        self.portal_overlay.place_forget()
        if not self.crashed:
            self.entry.configure(state="normal")
            self.entry.focus_set()

    @staticmethod
    def _ts() -> str:
        return time.strftime("%H:%M:%S")

    def _on_close(self) -> None:
        self._cancel_sequence_jobs()
        if self.crash_timer_job:
            self.root.after_cancel(self.crash_timer_job)
        if self.crash_anim_job:
            self.root.after_cancel(self.crash_anim_job)
        if self.portal_anim_job:
            self.root.after_cancel(self.portal_anim_job)
        if self.portal_close_job:
            self.root.after_cancel(self.portal_close_job)
        self.net.stop()
        self.root.destroy()


def prompt_server_ip(port: int) -> str:
    while True:
        raw = input(f"Server LAN IP (port {port}): ").strip()
        try:
            ipaddress.ip_address(raw)
        except ValueError:
            print("[ERROR] Enter a valid IPv4/IPv6 address, e.g. 192.168.1.44")
            continue
        if check_server_reachable(raw, port):
            print(f"[OK] Server reachable at {raw}:{port}")
            return raw
        print(f"[ERROR] Could not connect to {raw}:{port}. Is the server running?")


def prompt_username() -> str:
    while True:
        name = input("Username: ").strip()
        if not name:
            print("[ERROR] Username cannot be empty.")
            continue
        if len(name) > 24:
            print("[ERROR] Username must be 24 characters or fewer.")
            continue
        return name


def check_server_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Terminal Chat Tkinter client")
    parser.add_argument("--host", help="Server host/IP (optional; prompts if omitted)")
    parser.add_argument("--port", default=5050, type=int, help="Server port")
    parser.add_argument("--name", help="Display name (optional; prompts if omitted)")
    parser.add_argument("--director", action="store_true", help="Enable director controls")
    args = parser.parse_args()

    host = args.host or prompt_server_ip(args.port)
    if not check_server_reachable(host, args.port):
        print(f"[ERROR] Could not reach server at {host}:{args.port}")
        return

    name = args.name or prompt_username()

    root = tk.Tk()
    net = NetworkClient(host, args.port, name)
    net.start()
    TerminalUI(root, net, name, director=args.director)
    root.mainloop()


if __name__ == "__main__":
    main()
