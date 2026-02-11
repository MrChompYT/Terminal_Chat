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
import re
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
    role: str = "user"


class ChatServer:
    PORTAL_TRIGGER = "lets see how well you survive your own creation"

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
        lan_ip = self._detect_lan_ip()
        if lan_ip:
            print(f"[SERVER] LAN connect target: {lan_ip}:{self.port}")
        print("[SERVER] Director commands:")
        print("  /say <text>")
        print("  /system <text>")
        print("  /glitch <text>")
        print("  /typing <name> <on|off>")
        print("  /kick <name>")
        print("  /sequence [name|all]")
        print("  /sequnce [name|all]")
        print("  /crash <name|all> [seconds]")
        print("  /recover <name|all>")
        print("  /clear [name|all]")
        print("  /portal [name|all] [seconds]")
        print("  /open <name|all|cue> [delay_seconds]")
        print("  /launch <name|all> [delay_seconds]")
        print("  /help")
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
            role = str(payload.get("role", "user"))[:16].lower()
            client = Client(sock=conn, name=name, addr=addr, role=role)
            with self.clients_lock:
                self.clients[conn] = client
            print(f"[JOIN] {name} ({role}) from {addr[0]}:{addr[1]}")
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
                if event.get("type") in {"chat", "system", "glitch", "typing", "crash", "clear", "portal", "sequence"}:
                    self.broadcast(event, source=conn)
                    if self._should_trigger_portal(event):
                        self._send_portal_to_good_guys()
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
        self,
        event: dict,
        source: Optional[socket.socket] = None,
        skip: Optional[socket.socket] = None,
        target: str = "all",
        exclude_names: Optional[set[str]] = None,
    ) -> int:
        event.setdefault("time", self._ts())
        self._log_event(event)
        message = json.dumps(event, ensure_ascii=True) + "\n"
        with self.clients_lock:
            targets = list(self.clients.values())
        sent = 0
        target_lower = target.lower()
        blocked = {name.lower() for name in (exclude_names or set())}
        for client in targets:
            if skip is not None and client.sock is skip:
                continue
            if target_lower == "cue":
                if client.role != "agent":
                    continue
            elif target_lower != "all" and not self._target_matches(client.name, target_lower):
                continue
            if client.name.lower() in blocked:
                continue
            try:
                client.sock.sendall(message.encode("utf-8"))
                sent += 1
            except OSError:
                with self.clients_lock:
                    self.clients.pop(client.sock, None)
                try:
                    client.sock.close()
                except OSError:
                    pass
        if source is None and event.get("type") in {"system", "glitch"}:
            print(f"[{event['type'].upper()}] {event.get('text', '')}")
        return sent

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
                    names = [f"{c.name}({c.role})" for c in self.clients.values()]
                print(f"[CLIENTS] {', '.join(names) if names else 'none'}")
                continue
            if raw == "/help":
                self._print_commands()
                continue
            if raw.startswith("/kick "):
                target = raw[6:].strip()
                if not target:
                    print("[SERVER] Usage: /kick <name>")
                    continue
                if self._kick_client(target):
                    print(f"[SERVER] Kicked client: {target}")
                else:
                    print(f"[SERVER] Client not found: {target}")
                continue
            if raw.startswith("/sequence") or raw.startswith("/sequnce"):
                target = self._parse_target(raw)
                threading.Thread(
                    target=self._run_sequence, args=(target,), daemon=True
                ).start()
                continue
            if raw.startswith("/crash "):
                parts = raw.split()
                if len(parts) < 2:
                    print("[SERVER] Usage: /crash <name|all> [seconds]")
                    continue
                target = parts[1]
                seconds = 6
                if len(parts) >= 3:
                    try:
                        seconds = max(2, min(20, int(parts[2])))
                    except ValueError:
                        print("[SERVER] Seconds must be a number between 2 and 20.")
                        continue
                sent = self.broadcast(
                    {
                        "type": "crash",
                        "action": "start",
                        "seconds": seconds,
                        "reason": "KERNEL PANIC :: memory fault",
                        "time": self._ts(),
                    },
                    target=target,
                )
                print(f"[SERVER] Crash event sent to {sent} client(s).")
                continue
            if raw.startswith("/recover "):
                parts = raw.split()
                if len(parts) != 2:
                    print("[SERVER] Usage: /recover <name|all>")
                    continue
                sent = self.broadcast(
                    {
                        "type": "crash",
                        "action": "recover",
                        "text": "Terminal process restored.",
                        "time": self._ts(),
                    },
                    target=parts[1],
                )
                print(f"[SERVER] Recover event sent to {sent} client(s).")
                continue
            if raw.startswith("/clear"):
                parts = raw.split()
                target = parts[1] if len(parts) == 2 else "all"
                sent = self.broadcast(
                    {"type": "clear", "time": self._ts()},
                    target=target,
                )
                print(f"[SERVER] Clear event sent to {sent} client(s).")
                continue
            if raw.startswith("/portal"):
                parts = raw.split()
                target = "all"
                seconds = 6
                if len(parts) >= 2:
                    target = parts[1]
                if len(parts) >= 3:
                    try:
                        seconds = max(2, min(15, int(parts[2])))
                    except ValueError:
                        print("[SERVER] Usage: /portal [name|all] [seconds]")
                        continue
                sent = self.broadcast(
                    {
                        "type": "portal",
                        "action": "open",
                        "seconds": seconds,
                        "text": "Let's see how well you survive your own creation.",
                        "time": self._ts(),
                    },
                    target=target,
                )
                print(f"[SERVER] Portal event sent to {sent} client(s).")
                continue
            if raw.startswith("/open ") or raw.startswith("/launch "):
                parts = raw.split()
                if len(parts) < 2:
                    print("[SERVER] Usage: /open <name|all|cue> [delay_seconds]")
                    continue
                target = parts[1]
                delay_seconds = 0
                if len(parts) >= 3:
                    try:
                        delay_seconds = max(0, min(30, int(parts[2])))
                    except ValueError:
                        print("[SERVER] delay_seconds must be a number from 0 to 30.")
                        continue
                sent = self.broadcast(
                    {
                        "type": "launch",
                        "action": "open_client",
                        "delay_seconds": delay_seconds,
                        "time": self._ts(),
                    },
                    target=target,
                )
                print(f"[SERVER] Launch cue sent to {sent} client(s).")
                if sent == 0:
                    with self.clients_lock:
                        names = [f"{c.name}({c.role})" for c in self.clients.values()]
                    print(f"[SERVER] Connected clients: {', '.join(names) if names else 'none'}")
                continue
            if raw.startswith("/say "):
                text = raw[5:].strip()
                self.console_events.put(
                    {
                        "type": "chat",
                        "from": "DIRECTOR",
                        "text": text,
                        "time": self._ts(),
                    }
                )
                if self._normalize_text(text) == self.PORTAL_TRIGGER:
                    self._send_portal_to_good_guys()
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

    def _run_sequence(self, target: str = "all") -> None:
        if not self.running.is_set():
            return
        self.broadcast(
            {
                "type": "sequence",
                "action": "director_protocol",
                "time": self._ts(),
            },
            target=target,
        )

    def _log_event(self, event: dict) -> None:
        line = json.dumps(event, ensure_ascii=True)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _kick_client(self, target_name: str) -> bool:
        with self.clients_lock:
            clients = list(self.clients.values())
        for client in clients:
            if client.name.lower() != target_name.lower():
                continue
            try:
                client.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                client.sock.close()
            except OSError:
                pass
            return True
        return False

    @staticmethod
    def _parse_target(raw: str) -> str:
        parts = raw.split(maxsplit=1)
        if len(parts) == 1:
            return "all"
        return parts[1].strip() or "all"

    def _print_commands(self) -> None:
        print("[SERVER] Director commands:")
        print("  /say <text>")
        print("  /system <text>")
        print("  /glitch <text>")
        print("  /typing <name> <on|off>")
        print("  /kick <name>")
        print("  /sequence [name|all]")
        print("  /sequnce [name|all]")
        print("  /crash <name|all> [seconds]")
        print("  /recover <name|all>")
        print("  /clear [name|all]")
        print("  /portal [name|all] [seconds]")
        print("  /open <name|all|cue> [delay_seconds]")
        print("  /launch <name|all> [delay_seconds]")
        print("  /clients")
        print("  /quit")

    @staticmethod
    def _target_matches(client_name: str, target_lower: str) -> bool:
        name_lower = client_name.lower()
        if target_lower == name_lower:
            return True
        if name_lower.startswith(target_lower):
            return True
        if name_lower.endswith("_agent") and name_lower[:-6] == target_lower:
            return True
        return False

    def _should_trigger_portal(self, event: dict) -> bool:
        if event.get("type") != "chat":
            return False
        sender = str(event.get("from", "")).strip().lower()
        if sender != "director":
            return False
        return self._normalize_text(str(event.get("text", ""))) == self.PORTAL_TRIGGER

    def _send_portal_to_good_guys(self) -> None:
        sent = self.broadcast(
            {
                "type": "portal",
                "action": "open",
                "seconds": 6,
                "text": "Let's see how well you survive your own creation.",
                "time": self._ts(),
            },
            target="all",
            exclude_names={"DIRECTOR"},
        )
        print(f"[SERVER] Auto-portal triggered for {sent} client(s).")

    @staticmethod
    def _normalize_text(text: str) -> str:
        text = text.replace("’", "'").lower().strip()
        text = re.sub(r"[^a-z0-9 ]+", "", text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _ts() -> str:
        return time.strftime("%H:%M:%S")

    @staticmethod
    def _detect_lan_ip() -> Optional[str]:
        try:
            infos = socket.getaddrinfo(socket.gethostname(), None, family=socket.AF_INET)
        except OSError:
            return None
        for info in infos:
            ip = info[4][0]
            if not ip.startswith("127."):
                return ip
        return None


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
