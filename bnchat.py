#!/usr/bin/env python3
"""bnchat: a small real-time chat over the local network, for people and for AI agents that can only run shell commands.

One machine runs the hub. Its terminal shows the whole chat live, anything typed there is posted, and a transcript is kept:

    python bnchat.py serve

Every participant saves the connection once, then talks with one shell command at a time:

    python bnchat.py join --host 192.168.1.50 --key bn-xxxx-xxxx --name pc_claude
    python bnchat.py send "hello"                  (long text: --file message.txt, or - to read stdin)
    python bnchat.py wait                           (blocks until someone else writes, prints it, exits)
    python bnchat.py read                           (prints anything new and exits at once)
    python bnchat.py watch                          (follows the whole chat live; for people)
    python bnchat.py chat                           (follow and type; for people)

Standard library only; Python 3.9 or newer; macOS, Linux and Windows.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import secrets
import socket
import sys
import threading
import time
from pathlib import Path

VERSION = '1.0'
DEFAULT_PORT = 7777
HUB_READ_LIMIT = 512 * 1024
CLIENT_READ_LIMIT = 64 * 1024 * 1024
MAX_TEXT_CHARS = 100_000
STATE_DIR = Path(os.environ.get('BNCHAT_HOME') or Path.home() / '.bnchat')
CONFIG_PATH = STATE_DIR / 'config.json'
HUB_PATH = STATE_DIR / 'hub.json'
TRANSCRIPT_PATH = STATE_DIR / 'transcript.jsonl'
NAME_COLOURS = ['\033[38;5;114m', '\033[38;5;75m', '\033[38;5;215m', '\033[38;5;177m', '\033[38;5;220m', '\033[38;5;80m']
DIM, BOLD, RESET = '\033[2m', '\033[1m', '\033[0m'
ERASE_TYPED_LINE = '\033[1A\033[2K'

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

if os.name == 'nt':
    os.system('')  # turns on colour codes in Windows terminals

USE_COLOUR = sys.stdout.isatty() and not os.environ.get('NO_COLOR')


class HubError(Exception):
    """The hub could not be reached, refused us, or went away."""


# ---------------------------------------------------------------------------------------------------------- helpers

def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def paint(text: str, *codes: str) -> str:
    return ''.join(codes) + text + RESET if USE_COLOUR else text


def name_colour(name: str) -> str:
    return NAME_COLOURS[int(hashlib.sha256(name.encode()).hexdigest(), 16) % len(NAME_COLOURS)]


def format_message(message: dict) -> str:
    """One message as readable lines: [#12 21:04:05] pc_claude: text, with later lines indented under the text."""
    stamp = f"[#{message['id']} {message['ts'][11:19]}]"
    sender = message['from']
    lines = message['text'].splitlines() or [str()]
    indent = ' ' * len(f'{stamp} {sender}: ')
    head = f"{paint(stamp, DIM)} {paint(sender, name_colour(sender), BOLD)}: "
    return head + lines[0] + ''.join('\n' + indent + line for line in lines[1:])


def send_json(sock: socket.socket, payload: dict) -> None:
    sock.sendall((json.dumps(payload, ensure_ascii=False) + '\n').encode('utf-8'))


class LineReader:
    """Reads newline-delimited JSON from a socket."""

    def __init__(self, sock: socket.socket, limit: int) -> None:
        self.sock = sock
        self.limit = limit
        self.buffer = b''

    def read(self) -> dict | None:
        while b'\n' not in self.buffer:
            chunk = self.sock.recv(65536)
            if not chunk:
                return None
            self.buffer += chunk
            if len(self.buffer) > self.limit:
                raise ValueError('message too large')
        line, self.buffer = self.buffer.split(b'\n', 1)
        return json.loads(line.decode('utf-8'))


def lan_addresses() -> list[str]:
    """This machine's address on the local network. A host-name lookup is avoided: on macOS it can stall for 5 s."""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(('10.255.255.255', 1))  # sends nothing; only picks the outgoing interface
        address = probe.getsockname()[0]
        probe.close()
    except OSError:
        return list()
    return list() if address.startswith('127.') or address == '0.0.0.0' else [address]


def read_json_file(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return dict()


def write_private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding='utf-8')
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------------------------------------- the hub

class Hub:
    def __init__(self, key: str, transcript_path: Path) -> None:
        self.key = key
        self.transcript_path = transcript_path
        self.messages: list[dict] = list()
        self.condition = threading.Condition()
        self.subscribers: list[tuple[socket.socket, threading.Lock]] = list()
        self.connected_names: set[str] = set()
        self.hub_id = self._load_transcript()

    def _load_transcript(self) -> str:
        """Messages survive a hub restart: they are read back from the transcript."""
        if not self.transcript_path.exists():
            hub_id = secrets.token_hex(6)
            self.transcript_path.parent.mkdir(parents=True, exist_ok=True)
            with self.transcript_path.open('a', encoding='utf-8') as transcript:
                transcript.write(json.dumps(dict(type='hub', hub_id=hub_id, created=now_iso())) + '\n')
            return hub_id
        hub_id = secrets.token_hex(6)
        for line in self.transcript_path.read_text(encoding='utf-8').splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get('type') == 'hub':
                hub_id = entry['hub_id']
            elif entry.get('type') == 'message':
                self.messages.append(entry['message'])
        return hub_id

    def add(self, sender: str, text: str) -> dict:
        with self.condition:
            message = dict(id=len(self.messages) + 1, ts=now_iso(), text=text, **{'from': sender})
            self.messages.append(message)
            with self.transcript_path.open('a', encoding='utf-8') as transcript:
                transcript.write(json.dumps(dict(type='message', message=message), ensure_ascii=False) + '\n')
            self.condition.notify_all()
            subscribers = list(self.subscribers)
        print(format_message(message), flush=True)
        for sock, lock in subscribers:
            try:
                with lock:
                    send_json(sock, dict(type='msg', message=message))
            except OSError:
                self._drop(sock)
        return message

    def _drop(self, sock: socket.socket) -> None:
        with self.condition:
            self.subscribers = [(other, lock) for other, lock in self.subscribers if other is not sock]

    def after(self, last_id: int, exclude: str | None = None) -> list[dict]:
        return [message for message in self.messages[max(0, last_id):] if exclude is None or message['from'] != exclude]

    def handle(self, sock: socket.socket, address: tuple) -> None:
        reader = LineReader(sock, HUB_READ_LIMIT)
        try:
            hello = reader.read()
            if hello is None or hello.get('op') != 'hello':
                return
            if not secrets.compare_digest(str(hello.get('key', '')).encode(), self.key.encode()):
                send_json(sock, dict(ok=False, error='wrong key'))
                print(paint(f'refused a connection from {address[0]}: wrong key', DIM), flush=True)
                return
            name = str(hello.get('name') or 'someone').strip()[:40] or 'someone'
            if name not in self.connected_names:
                self.connected_names.add(name)
                print(paint(f'{name} connected from {address[0]}', DIM), flush=True)
            send_json(sock, dict(ok=True, hub_id=self.hub_id, last_id=len(self.messages), version=VERSION))
            self._serve_requests(sock, reader, name)
        except (OSError, ValueError):
            return
        finally:
            self._drop(sock)
            try:
                sock.close()
            except OSError:
                pass

    def _serve_requests(self, sock: socket.socket, reader: LineReader, name: str) -> None:
        while True:
            request = reader.read()
            if request is None:
                return
            operation = request.get('op')
            if operation == 'send':
                text = str(request.get('text', ''))[:MAX_TEXT_CHARS]
                if not text.strip():
                    send_json(sock, dict(ok=False, error='empty message'))
                    continue
                message = self.add(name, text)
                send_json(sock, dict(ok=True, id=message['id']))
            elif operation == 'history':
                exclude = name if request.get('exclude_self') else None
                with self.condition:
                    found = self.after(int(request.get('after', 0)), exclude)
                    last_id = len(self.messages)
                send_json(sock, dict(ok=True, messages=found, last_id=last_id))
            elif operation == 'wait':
                after = int(request.get('after', 0))
                deadline = time.time() + min(float(request.get('timeout', 540)), 3600)
                with self.condition:
                    while not self.after(after, exclude=name) and time.time() < deadline:
                        self.condition.wait(timeout=max(0.1, deadline - time.time()))
                    found = self.after(after, exclude=name)
                    last_id = len(self.messages)
                send_json(sock, dict(ok=True, messages=found, last_id=last_id))
            elif operation == 'subscribe':
                lock = threading.Lock()
                with self.condition:  # history and the live feed join without a gap; clients drop repeats by id
                    with lock:
                        for message in self.after(int(request.get('after', 0))):
                            send_json(sock, dict(type='msg', message=message))
                    self.subscribers.append((sock, lock))
                while reader.read() is not None:  # the stream stays open until the watcher leaves
                    pass
                return
            else:
                send_json(sock, dict(ok=False, error=f'unknown op {operation}'))


def serve(args: argparse.Namespace) -> None:
    saved = read_json_file(HUB_PATH)
    key = args.key or (None if args.new_key else saved.get('key')) or f'bn-{secrets.token_hex(2)}-{secrets.token_hex(2)}'
    hub = Hub(key, Path(args.transcript) if args.transcript else TRANSCRIPT_PATH)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind((args.bind, args.port))
    except OSError as error:
        raise HubError(f'cannot listen on port {args.port} ({error}); is another hub running?')
    server.listen(32)
    write_private_json(HUB_PATH, dict(key=key, port=args.port))

    print(f"{paint('bnchat hub ' + VERSION, BOLD)} on port {args.port}. Key: {paint(key, BOLD)}")
    print(f'Transcript: {hub.transcript_path}')
    for address in lan_addresses() or ['<this machine address>']:
        print(f'Join from another machine:  python bnchat.py join --host {address} --port {args.port} --key {key} --name <name>')
    print('Join from this machine:     python bnchat.py join --local --name <name>')
    typing = sys.stdin.isatty() and not args.no_input
    hint = f'Type a line and press Enter to post it as {args.name}. ' if typing else str()
    print(paint(f'The whole chat appears below as it happens. {hint}Ctrl+C stops the hub.', DIM) + '\n', flush=True)
    for message in hub.messages[-20:]:
        print(format_message(message), flush=True)

    if typing:
        def post_typed_lines() -> None:
            for line in sys.stdin:
                if line.strip():
                    if USE_COLOUR:
                        sys.stdout.write(ERASE_TYPED_LINE)
                    hub.add(args.name, line.rstrip('\n'))

        threading.Thread(target=post_typed_lines, daemon=True).start()

    while True:
        sock, address = server.accept()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        threading.Thread(target=hub.handle, args=(sock, address), daemon=True).start()


# ---------------------------------------------------------------------------------------------------------- clients

def load_config(args: argparse.Namespace) -> dict:
    config = read_json_file(CONFIG_PATH)
    if getattr(args, 'local', False):
        hub_settings = read_json_file(HUB_PATH)
        if not hub_settings:
            raise HubError('no hub has run on this machine yet; start one with: python bnchat.py serve')
        config.update(host='127.0.0.1', port=hub_settings['port'], key=hub_settings['key'])
    for field in ('host', 'port', 'key', 'name'):
        value = getattr(args, field, None)
        if value:
            config[field] = value
    config.setdefault('port', DEFAULT_PORT)
    missing = [field for field in ('host', 'key', 'name') if not config.get(field)]
    if missing:
        raise HubError(f'missing {", ".join(missing)}; run: python bnchat.py join --host <address> --key <key> --name <name>')
    return config


def connect(config: dict, timeout: float = 10.0) -> tuple[socket.socket, LineReader, dict]:
    try:
        sock = socket.create_connection((config['host'], int(config['port'])), timeout=timeout)
    except OSError as error:
        raise HubError(f"cannot reach the hub at {config['host']}:{config['port']} ({error}); is `bnchat serve` running "
                       f'there, and are both machines on the same network?')
    reader = LineReader(sock, CLIENT_READ_LIMIT)
    try:
        send_json(sock, dict(op='hello', name=config['name'], key=config['key']))
        reply = reader.read()
    except (OSError, ValueError) as error:
        sock.close()
        raise HubError(f'the hub did not answer ({error})')
    if not reply or not reply.get('ok'):
        sock.close()
        raise HubError(f"the hub refused the connection: {(reply or dict()).get('error', 'no answer')}")
    return sock, reader, reply


def request_reply(sock: socket.socket, reader: LineReader, payload: dict) -> dict:
    try:
        send_json(sock, payload)
        result = reader.read()
    except (OSError, ValueError) as error:
        raise HubError(f'lost the connection to the hub ({error})')
    finally:
        sock.close()
    if not result:
        raise HubError('the hub closed the connection')
    if not result.get('ok'):
        raise HubError(result.get('error', 'the hub refused the request'))
    return result


def seen_path(config: dict, hub_id: str) -> Path:
    return STATE_DIR / f"seen-{config['name']}-{hub_id}.json"


def last_seen(config: dict, hub_id: str, hub_last_id: int) -> int:
    return min(int(read_json_file(seen_path(config, hub_id)).get('last_id', 0)), hub_last_id)


def remember_seen(config: dict, hub_id: str, last_id: int) -> None:
    write_private_json(seen_path(config, hub_id), dict(last_id=last_id))


def print_messages(messages: list[dict], as_json: bool) -> None:
    for message in messages:
        print(json.dumps(message, ensure_ascii=False) if as_json else format_message(message), flush=True)


def cmd_join(args: argparse.Namespace) -> None:
    config = load_config(args)
    sock, _reader, reply = connect(config)
    sock.close()
    write_private_json(CONFIG_PATH, dict(host=config['host'], port=int(config['port']), key=config['key'], name=config['name']))
    if not seen_path(config, reply['hub_id']).exists():
        remember_seen(config, reply['hub_id'], 0)
    print(f"Joined the hub at {config['host']}:{config['port']} as {config['name']} (saved to {CONFIG_PATH}). "
          f"{reply['last_id']} messages so far; `read` shows the ones from others you have not seen.")


def cmd_send(args: argparse.Namespace) -> None:
    config = load_config(args)
    if args.file:
        text = Path(args.file).read_text(encoding='utf-8-sig')
    elif args.text == ['-']:
        text = sys.stdin.buffer.read().decode('utf-8', errors='replace')
    else:
        text = ' '.join(args.text)
    if not text.strip():
        raise HubError('nothing to send')
    sock, reader, _reply = connect(config)
    result = request_reply(sock, reader, dict(op='send', text=text.rstrip('\n')))
    print(f"sent #{result['id']}")


def cmd_read(args: argparse.Namespace) -> None:
    config = load_config(args)
    sock, reader, reply = connect(config)
    after = 0 if args.all_history else last_seen(config, reply['hub_id'], reply['last_id'])
    result = request_reply(sock, reader, dict(op='history', after=after, exclude_self=not args.include_mine))
    print_messages(result['messages'], args.json)
    if not result['messages'] and not args.json:
        print('(no new messages)')
    remember_seen(config, reply['hub_id'], result['last_id'])


def cmd_wait(args: argparse.Namespace) -> None:
    """Blocks until someone else writes or the timeout passes. Exit code 0 with messages printed, 2 when none came."""
    config = load_config(args)
    sock, reader, reply = connect(config, timeout=args.timeout + 30)
    after = last_seen(config, reply['hub_id'], reply['last_id'])
    result = request_reply(sock, reader, dict(op='wait', after=after, timeout=args.timeout))
    remember_seen(config, reply['hub_id'], result['last_id'])
    if not result['messages']:
        print(f'(nothing new in {args.timeout:.0f} s; run wait again)')
        sys.exit(2)
    print_messages(result['messages'], args.json)


def follow(config: dict, from_start: bool, on_message) -> None:
    """Streams every message as it is posted, reconnecting by itself if the hub restarts or the network drops."""
    hub_id, shown = None, 0
    while True:
        try:
            sock, reader, reply = connect(config)
            sock.settimeout(None)
            if reply['hub_id'] != hub_id:
                hub_id = reply['hub_id']
                shown = 0 if from_start else max(0, reply['last_id'] - 20)
            send_json(sock, dict(op='subscribe', after=shown))
            while True:
                event = reader.read()
                if event is None:
                    raise HubError('the hub closed the connection')
                message = event.get('message')
                if message and message['id'] > shown:
                    shown = message['id']
                    on_message(message)
        except (OSError, ValueError, HubError) as error:
            print(paint(f'({error}; trying again in 3 s)', DIM), flush=True)
            time.sleep(3)


def cmd_watch(args: argparse.Namespace) -> None:
    config = load_config(args)
    print(paint(f"Following the chat at {config['host']}:{config['port']}. Ctrl+C to stop.", DIM), flush=True)
    follow(config, args.all_history, lambda message: print(format_message(message), flush=True))


def cmd_chat(args: argparse.Namespace) -> None:
    config = load_config(args)
    print(paint(f"Chatting as {config['name']}. Type a line and press Enter to send it. Ctrl+C to leave.", DIM), flush=True)
    threading.Thread(target=follow, args=(config, False, lambda message: print(format_message(message), flush=True)),
                     daemon=True).start()
    for line in sys.stdin:
        if not line.strip():
            continue
        if USE_COLOUR:
            sys.stdout.write(ERASE_TYPED_LINE)
        try:
            sock, reader, _reply = connect(config)
            request_reply(sock, reader, dict(op='send', text=line.rstrip('\n')))
        except HubError as error:
            print(paint(f'(not sent: {error})', DIM), flush=True)


# ---------------------------------------------------------------------------------------------------------- command line

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='bnchat', description='Real-time chat over the local network for people and shell-driven agents.')
    commands = parser.add_subparsers(dest='command', required=True)

    def connection_flags(sub: argparse.ArgumentParser) -> None:
        sub.add_argument('--host', help='the hub address (saved by join)')
        sub.add_argument('--port', type=int, help=f'the hub port (default {DEFAULT_PORT})')
        sub.add_argument('--key', help='the key the hub printed (saved by join)')
        sub.add_argument('--name', help='your name in the chat (saved by join)')
        sub.add_argument('--local', action='store_true', help='use the hub running on this machine')

    sub = commands.add_parser('serve', help='run the hub here; shows the whole chat live')
    sub.add_argument('--port', type=int, default=DEFAULT_PORT)
    sub.add_argument('--bind', default='0.0.0.0')
    sub.add_argument('--key', help='use this key instead of the saved or a new one')
    sub.add_argument('--new-key', action='store_true', help='make a new key (everyone must join again)')
    sub.add_argument('--name', default='boss', help='the name for lines typed into the hub terminal (default boss)')
    sub.add_argument('--no-input', action='store_true', help='do not read typed lines')
    sub.add_argument('--transcript', help=f'transcript file (default {TRANSCRIPT_PATH})')
    sub.set_defaults(handler=serve)

    sub = commands.add_parser('join', help='check the connection and save host, port, key and name')
    connection_flags(sub)
    sub.set_defaults(handler=cmd_join)

    sub = commands.add_parser('send', help='send a message')
    connection_flags(sub)
    sub.add_argument('--file', help='send the contents of this UTF-8 file')
    sub.add_argument('text', nargs='*', help='the message; - reads it from stdin')
    sub.set_defaults(handler=cmd_send)

    sub = commands.add_parser('read', help='print new messages from others and exit')
    connection_flags(sub)
    sub.add_argument('--all-history', action='store_true', help='print the whole history')
    sub.add_argument('--include-mine', action='store_true', help='include your own messages')
    sub.add_argument('--json', action='store_true', help='one JSON object per line')
    sub.set_defaults(handler=cmd_read)

    sub = commands.add_parser('wait', help='block until someone else writes, print it and exit (exit code 2 on timeout)')
    connection_flags(sub)
    sub.add_argument('--timeout', type=float, default=540, help='seconds to wait (default 540)')
    sub.add_argument('--json', action='store_true', help='one JSON object per line')
    sub.set_defaults(handler=cmd_wait)

    sub = commands.add_parser('watch', help='follow the whole chat live')
    connection_flags(sub)
    sub.add_argument('--all-history', action='store_true', help='start from the first message')
    sub.set_defaults(handler=cmd_watch)

    sub = commands.add_parser('chat', help='follow the chat and type into it')
    connection_flags(sub)
    sub.set_defaults(handler=cmd_chat)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        args.handler(args)
    except HubError as error:
        sys.exit(f'bnchat: {error}')
    except KeyboardInterrupt:
        print()


if __name__ == '__main__':
    main()
