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
import queue
import random
import socket
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Optional


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

        self._build_chrome(frame)
        self._build_terminal(frame)
        self._build_input(frame)
        if self.director:
            self._build_director_panel(frame)

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
            steps = [
                {"type": "system", "text": "Handshake replay started...", "time": self._ts()},
                {"type": "typing", "from": "REMOTE", "state": True, "time": self._ts()},
                {"type": "chat", "from": "REMOTE", "text": "you shouldn't be here", "time": self._ts()},
                {"type": "typing", "from": "REMOTE", "state": False, "time": self._ts()},
                {"type": "glitch", "text": "IO-FAILURE x9x9x9", "time": self._ts()},
            ]
            for event in steps:
                self.net.send(event)
                time.sleep(0.9)

        threading.Thread(target=worker, daemon=True).start()

    def _on_enter(self, _: tk.Event) -> None:
        self._send_message()

    def _send_message(self) -> None:
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

    @staticmethod
    def _ts() -> str:
        return time.strftime("%H:%M:%S")

    def _on_close(self) -> None:
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
