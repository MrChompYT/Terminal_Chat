#!/usr/bin/env python3
"""
Launch agent for planned "random" client startup during filming.

Run this on the target laptop. It maintains an outbound connection to the
server and waits for a `launch` event, then opens `client.py`.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional


class LaunchAgent:
    def __init__(
        self,
        server_host: str,
        server_port: int,
        agent_name: str,
        chat_host: str,
        chat_port: int,
        chat_name: str,
        workdir: Path,
    ) -> None:
        self.server_host = server_host
        self.server_port = server_port
        self.agent_name = agent_name
        self.chat_host = chat_host
        self.chat_port = chat_port
        self.chat_name = chat_name
        self.workdir = workdir
        self.proc: Optional[subprocess.Popen] = None

    def run(self) -> None:
        print(
            f"[AGENT] Connecting to {self.server_host}:{self.server_port} as {self.agent_name}"
        )
        while True:
            try:
                self._session()
            except OSError:
                print("[AGENT] Connection lost. Reconnecting...")
                time.sleep(1.0)

    def _session(self) -> None:
        with socket.create_connection((self.server_host, self.server_port), timeout=6) as sock:
            hello = json.dumps({"type": "hello", "name": self.agent_name}) + "\n"
            sock.sendall(hello.encode("utf-8"))
            file = sock.makefile("r", encoding="utf-8", newline="\n")
            print("[AGENT] Connected and waiting for /open cue.")
            for line in file:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._handle_event(event)

    def _handle_event(self, event: dict) -> None:
        if event.get("type") != "launch":
            return
        if event.get("action") != "open_client":
            return
        delay = int(event.get("delay_seconds", 0))
        if delay > 0:
            print(f"[AGENT] Launch cue received. Waiting {delay}s...")
            time.sleep(delay)
        self._launch_client()

    def _launch_client(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            print("[AGENT] client.py already running.")
            return
        command = [
            sys.executable,
            "client.py",
            "--host",
            self.chat_host,
            "--port",
            str(self.chat_port),
            "--name",
            self.chat_name,
        ]
        print("[AGENT] Starting client.py")
        self.proc = subprocess.Popen(command, cwd=str(self.workdir))


def main() -> None:
    parser = argparse.ArgumentParser(description="Remote launch agent for client.py")
    parser.add_argument("--server-host", required=True, help="Server host/IP for cue channel")
    parser.add_argument("--server-port", type=int, default=5050, help="Server port for cue channel")
    parser.add_argument("--agent-name", required=True, help="Unique agent name, e.g. HERO1_AGENT")
    parser.add_argument("--chat-host", required=True, help="Host for launched client.py")
    parser.add_argument("--chat-port", type=int, default=5050, help="Port for launched client.py")
    parser.add_argument("--chat-name", required=True, help="Username for launched client.py")
    parser.add_argument(
        "--workdir",
        default=".",
        help="Directory containing client.py (default: current directory)",
    )
    args = parser.parse_args()

    agent = LaunchAgent(
        server_host=args.server_host,
        server_port=args.server_port,
        agent_name=args.agent_name,
        chat_host=args.chat_host,
        chat_port=args.chat_port,
        chat_name=args.chat_name,
        workdir=Path(args.workdir).resolve(),
    )
    agent.run()


if __name__ == "__main__":
    main()
