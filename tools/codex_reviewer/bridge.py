#!/usr/bin/env python3
"""Keep a local record of requests and replies for a manually relayed PR audit."""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import sys
import unicodedata
import uuid

ROOT = Path(__file__).resolve().parents[2]
PR_URL = re.compile(r"https://github\.com/AnatoliyZakharov/ZinaidaSergeevnaBot/pull/[1-9][0-9]{0,11}")
HEAD = re.compile(r"[0-9a-f]{40}")
REQUEST_ID = re.compile(r"[0-9a-f]{32}")
VERDICTS = ("PASS", "FAIL", "BLOCKED")
MAX_REPLY_BYTES = 32768
MAX_STATE_BYTES = MAX_REPLY_BYTES + 1024
PRIVATE_DIR = 0o700
PRIVATE_FILE = 0o600


class ProtocolError(Exception):
    """An error with a fixed safe message."""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "Invalid bridge arguments. See --help.\n")


def checked(pattern, value, error):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ProtocolError(error)
    return value


def encode(payload):
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def unique_fields(pairs):
    payload = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("duplicate JSON field")
        payload[key] = value
    return payload


def decode(raw, limit, error):
    if len(raw) > limit:
        raise ProtocolError(error)
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_fields)
    except (ValueError, RecursionError):
        raise ProtocolError(error) from None


def validate_reply(reply, request_id, head):
    if not isinstance(reply, dict) or set(reply) != {"request_id", "head_sha", "verdict", "findings"}:
        raise ProtocolError("Invalid audit reply fields.")
    if reply["request_id"] != request_id or reply["head_sha"] != head:
        raise ProtocolError("Audit reply does not match the request ID and head SHA.")
    if reply["verdict"] not in VERDICTS:
        raise ProtocolError("Invalid audit verdict.")
    findings = reply["findings"]
    if not isinstance(findings, list) or len(findings) > 20:
        raise ProtocolError("Invalid audit findings.")
    for line in findings:
        if (not isinstance(line, str) or not 1 <= len(line) <= 1000
                or any(unicodedata.category(char).startswith("C") for char in line)):
            raise ProtocolError("Invalid audit findings.")


def check_private(info, kind, mode):
    if stat.S_IFMT(info.st_mode) != kind or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != mode:
        raise ProtocolError("Bridge state requires a private directory (0700) and private files (0600).")


class State:
    def __init__(self, descriptor):
        self.descriptor = descriptor

    def contains(self, request_id):
        try:
            os.stat(request_id + ".json", dir_fd=self.descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    def read(self, request_id):
        try:
            descriptor = os.open(request_id + ".json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=self.descriptor)
        except FileNotFoundError:
            raise ProtocolError("Unknown audit request.") from None
        with os.fdopen(descriptor, "rb") as handle:
            check_private(os.fstat(handle.fileno()), stat.S_IFREG, PRIVATE_FILE)
            record = decode(handle.read(MAX_STATE_BYTES + 1), MAX_STATE_BYTES, "Invalid bridge state.")
        if not isinstance(record, dict) or set(record) != {"request_id", "head_sha", "pr_url", "reply"}:
            raise ProtocolError("Invalid bridge state.")
        if record["request_id"] != request_id:
            raise ProtocolError("Invalid bridge state.")
        checked(HEAD, record["head_sha"], "Invalid bridge state.")
        checked(PR_URL, record["pr_url"], "Invalid bridge state.")
        if record["reply"] is not None:
            validate_reply(record["reply"], request_id, record["head_sha"])
        return record

    def write(self, record):
        scratch = ".tmp-" + uuid.uuid4().hex
        descriptor = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                             PRIVATE_FILE, dir_fd=self.descriptor)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                os.fchmod(handle.fileno(), PRIVATE_FILE)
                handle.write(encode(record))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(scratch, record["request_id"] + ".json",
                       src_dir_fd=self.descriptor, dst_dir_fd=self.descriptor)
            os.fsync(self.descriptor)
        finally:
            try:
                os.unlink(scratch, dir_fd=self.descriptor)
            except FileNotFoundError:
                pass

    def records(self):
        for name in sorted(os.listdir(self.descriptor)):
            if name.endswith(".json") and REQUEST_ID.fullmatch(name[:-5]):
                yield self.read(name[:-5])


@contextmanager
def open_state(path, create=False):
    if not path.is_absolute():
        raise ProtocolError("Use an absolute state directory outside any Git checkout.")
    resolved = path.resolve()
    if (resolved.is_relative_to(ROOT)
            or any(os.path.lexists(parent / ".git") for parent in (resolved, *resolved.parents))):
        raise ProtocolError("Use an absolute state directory outside any Git checkout.")
    if path.is_symlink():
        raise ProtocolError("Bridge state directory cannot be a symlink.")
    if create:
        resolved.mkdir(parents=True, mode=PRIVATE_DIR, exist_ok=True)
    descriptor = os.open(resolved, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        check_private(os.fstat(descriptor), stat.S_IFDIR, PRIVATE_DIR)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield State(descriptor)
    finally:
        os.close(descriptor)


def metadata(record):
    return {key: record[key] for key in ("request_id", "pr_url", "head_sha")}


def status(record):
    return dict(metadata(record), status="pending" if record["reply"] is None else "received")


def run(args):
    if args.command == "request":
        url = checked(PR_URL, args.pr_url, "Use the project's exact GitHub PR URL.")
        head = checked(HEAD, args.head_sha, "Use a full lowercase head SHA.")
        with open_state(args.state_dir, create=True) as state:
            request = {"request_id": uuid.uuid4().hex, "pr_url": url, "head_sha": head}
            # Directory lock covers this check and publication.
            while state.contains(request["request_id"]):
                request["request_id"] = uuid.uuid4().hex
            state.write(dict(request, reply=None))
            return request

    if args.command == "status" and args.request_id is None:
        try:
            with open_state(args.state_dir) as state:
                return [status(record) for record in state.records()]
        except FileNotFoundError:
            return []

    request_id = checked(REQUEST_ID, args.request_id, "Invalid audit request ID.")
    if args.command in ("import", "result"):
        head = checked(HEAD, args.current_head, "Use a full lowercase head SHA.")
    if args.command == "import":
        reply = decode(sys.stdin.buffer.read(MAX_REPLY_BYTES + 1), MAX_REPLY_BYTES, "Invalid audit reply JSON.")
        validate_reply(reply, request_id, head)

    try:
        with open_state(args.state_dir) as state:
            record = state.read(request_id)
            if args.command == "status":
                return status(record)
            if record["head_sha"] != head:
                raise ProtocolError("Audit request is stale for the supplied current head SHA.")
            if args.command == "import":
                if record["reply"] is None:
                    record["reply"] = reply
                    state.write(record)
                elif record["reply"] != reply:
                    raise ProtocolError("A different reply is already recorded for this request.")
                return status(record)
            if record["reply"] is None:
                raise ProtocolError("Audit reply is pending.")
            return record["reply"]
    except FileNotFoundError:
        raise ProtocolError("Unknown audit request.") from None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["hook"]:
        # Keep a previously installed local PermissionRequest observer inert.
        return 0

    parser = Parser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".local/state/zinochka-codex-reviewer")
    commands = parser.add_subparsers(dest="command", required=True)
    request = commands.add_parser("request", help="create a pending audit request")
    request.add_argument("pr_url")
    request.add_argument("head_sha")
    view = commands.add_parser("status", help="list request metadata or show one request")
    view.add_argument("request_id", nargs="?")
    for name in ("import", "result"):
        command = commands.add_parser(name)
        command.add_argument("request_id")
        command.add_argument("current_head")
    args = parser.parse_args(argv)
    try:
        sys.stdout.buffer.write(encode(run(args)))
        sys.stdout.buffer.flush()
    except ProtocolError as error:
        print(error, file=sys.stderr)
        return 1
    except (OSError, RuntimeError):
        print("Bridge state or input/output unavailable.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Bridge interrupted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
