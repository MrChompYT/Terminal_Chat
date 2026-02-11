#!/usr/bin/env python3
"""
Terminal Chat server.

Two-client-friendly message hub with director controls:
- JSON-lines TCP protocol
- Broadcast chat and cinematic events
- Session logging for retakes
"""

from __future__ import annotations

import argparse
import json
import queue
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional


@dataclass
class Client:
    sock: socket.socket
    name: str
    addr: tuple[str, int]


class ChatServer:
    def __init__(self, host: str, port: int, log_file: Path) -> None:
        self.host = host
        self.port = port
        self.log_file = log_file
        self.server_sock: Optional[socket.socket] = None
        self.clients: Dict[socket.socket, Client] = {}
        self.clients_lock = threading.Lock()
        self.running = threading.Event()
        self.console_events: "queue.Queue[dict]" = queue.Queue()

    def start(self) -> None:
        self.running.set()
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.bind((self.host, self.port))
        self.server_sock.listen()
        print(f"[SERVER] Listening on {self.host}:{self.port}")
        print("[SERVER] Director commands:")
        print("  /say <text>")
        print("  /system <text>")
        print("  /glitch <text>")
        print("  /typing <name> <on|off>")
        print("  /sequence")
        print("  /clients")
        print("  /quit")

        threading.Thread(target=self._accept_loop, daemon=True).start()
        threading.Thread(target=self._director_console_loop, daemon=True).start()

        while self.running.is_set():
            try:
                event = self.console_events.get(timeout=0.2)
            except queue.Empty:
                continue
            self.broadcast(event)

    def stop(self) -> None:
        self.running.clear()
        if self.server_sock:
            try:
                self.server_sock.close()
            except OSError:
                pass
        with self.clients_lock:
            clients = list(self.clients.values())
            self.clients.clear()
        for client in clients:
            try:
                client.sock.close()
            except OSError:
                pass
        print("[SERVER] Stopped.")

    def _accept_loop(self) -> None:
        assert self.server_sock is not None
        while self.running.is_set():
            try:
                conn, addr = self.server_sock.accept()
            except OSError:
                break
            threading.Thread(
                target=self._handle_client, args=(conn, addr), daemon=True
            ).start()

    def _handle_client(self, conn: socket.socket, addr: tuple[str, int]) -> None:
        file = conn.makefile("r", encoding="utf-8", newline="\n")
        client: Optional[Client] = None
        try:
            hello = file.readline()
            if not hello:
                conn.close()
                return
            payload = json.loads(hello)
            if payload.get("type") != "hello":
                conn.close()
                return
            name = str(payload.get("name", "unknown"))[:32]
            client = Client(sock=conn, name=name, addr=addr)
            with self.clients_lock:
                self.clients[conn] = client
            print(f"[JOIN] {name} from {addr[0]}:{addr[1]}")
            self._log_event(
                {"type": "system", "text": f"{name} connected", "time": self._ts()}
            )
            self.broadcast(
                {"type": "system", "text": f"{name} joined.", "time": self._ts()},
                skip=conn,
            )

            while self.running.is_set():
                line = file.readline()
                if not line:
                    break
                event = json.loads(line)
                event.setdefault("time", self._ts())
                if event.get("type") in {"chat", "system", "glitch", "typing"}:
                    self.broadcast(event, source=conn)
        except (OSError, json.JSONDecodeError):
            pass
        finally:
            if client is not None:
                with self.clients_lock:
                    self.clients.pop(conn, None)
                print(f"[LEAVE] {client.name}")
                self.broadcast({"type": "system", "text": f"{client.name} left."})
                self._log_event(
                    {
                        "type": "system",
                        "text": f"{client.name} disconnected",
                        "time": self._ts(),
                    }
                )
            try:
                conn.close()
            except OSError:
                pass

    def broadcast(
        self, event: dict, source: Optional[socket.socket] = None, skip: Optional[socket.socket] = None
    ) -> None:
        event.setdefault("time", self._ts())
        self._log_event(event)
        message = json.dumps(event, ensure_ascii=True) + "\n"
        with self.clients_lock:
            targets = list(self.clients.values())
        for client in targets:
            if skip is not None and client.sock is skip:
                continue
            try:
                client.sock.sendall(message.encode("utf-8"))
            except OSError:
                with self.clients_lock:
                    self.clients.pop(client.sock, None)
                try:
                    client.sock.close()
                except OSError:
                    pass
        if source is None and event.get("type") in {"system", "glitch"}:
            print(f"[{event['type'].upper()}] {event.get('text', '')}")

    def _director_console_loop(self) -> None:
        while self.running.is_set():
            try:
                raw = input("> ").strip()
            except EOFError:
                self.stop()
                break
            if not raw:
                continue
            if raw == "/quit":
                self.stop()
                break
            if raw == "/clients":
                with self.clients_lock:
                    names = [c.name for c in self.clients.values()]
                print(f"[CLIENTS] {', '.join(names) if names else 'none'}")
                continue
            if raw == "/sequence":
                threading.Thread(target=self._run_sequence, daemon=True).start()
                continue
            if raw.startswith("/say "):
                self.console_events.put(
                    {
                        "type": "chat",
                        "from": "DIRECTOR",
                        "text": raw[5:].strip(),
                        "time": self._ts(),
                    }
                )
                continue
            if raw.startswith("/system "):
                self.console_events.put(
                    {"type": "system", "text": raw[8:].strip(), "time": self._ts()}
                )
                continue
            if raw.startswith("/glitch "):
                self.console_events.put(
                    {"type": "glitch", "text": raw[8:].strip(), "time": self._ts()}
                )
                continue
            if raw.startswith("/typing "):
                parts = raw.split(maxsplit=2)
                if len(parts) != 3 or parts[2] not in {"on", "off"}:
                    print("[SERVER] Usage: /typing <name> <on|off>")
                    continue
                self.console_events.put(
                    {
                        "type": "typing",
                        "from": parts[1],
                        "state": parts[2] == "on",
                        "time": self._ts(),
                    }
                )
                continue
            print("[SERVER] Unknown command.")

    def _run_sequence(self) -> None:
        sequence = [
            {"type": "system", "text": "Link stability check initiated..."},
            {"type": "typing", "from": "REMOTE", "state": True},
            {"type": "chat", "from": "REMOTE", "text": "Do you copy?"},
            {"type": "typing", "from": "REMOTE", "state": False},
            {"type": "glitch", "text": "SIGNAL NOISE :: #### 4F 2A 9C"},
            {"type": "system", "text": "Unauthorized probe detected."},
        ]
        for event in sequence:
            if not self.running.is_set():
                return
            event["time"] = self._ts()
            self.broadcast(event)
            time.sleep(1.1)

    def _log_event(self, event: dict) -> None:
        line = json.dumps(event, ensure_ascii=True)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    @staticmethod
    def _ts() -> str:
        return time.strftime("%H:%M:%S")


def main() -> None:
    parser = argparse.ArgumentParser(description="Terminal Chat server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", default=5050, type=int, help="Bind port")
    parser.add_argument(
        "--log-file",
        default="logs/session.jsonl",
        help="Session log path (JSONL)",
    )
    args = parser.parse_args()

    server = ChatServer(args.host, args.port, Path(args.log_file))
    try:
        server.start()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


if __name__ == "__main__":
    main()
