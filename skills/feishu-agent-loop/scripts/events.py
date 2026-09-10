#!/usr/bin/env python3
"""Validate local routing configuration, IM events, and send receipts.

Routing produces candidates, never permission to execute an action. This module
uses only the standard library and does not call Lark or execute message text.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid

MAX_BYTES = 1024 * 1024


class InputError(ValueError):
    pass


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError("duplicate JSON key")
        result[key] = value
    return result


def decode(raw):
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        return json.loads(text, object_pairs_hook=unique_object)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise InputError("invalid UTF-8 or JSON") from error


def read_limited(stream):
    raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise InputError("input exceeds 1 MiB; retain it for manual inspection")
    return raw


def read_file(path):
    with open(path, "rb") as stream:
        return read_limited(stream)


def identifier(value, prefix):
    return (isinstance(value, str) and len(value) <= 200
            and re.fullmatch(prefix + r"[A-Za-z0-9_-]+", value) is not None)


def load_config(path, line):
    config = decode(read_file(path))
    required = {"schema_version", "lines", "allowed_senders", "groups"}
    if not isinstance(config, dict) or set(config) != required:
        raise InputError("config must contain exactly schema_version, lines, allowed_senders, groups")
    if type(config["schema_version"]) is not int or config["schema_version"] != 1:
        raise InputError("unsupported config schema_version")
    lines = config["lines"]
    if (not isinstance(lines, list) or not lines
            or any(not isinstance(x, str) or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", x) is None for x in lines)
            or len(lines) != len(set(lines)) or line not in lines):
        raise InputError("invalid lines or unregistered --line")
    senders = config["allowed_senders"]
    if (not isinstance(senders, list) or not senders
            or any(not identifier(x, "ou_") for x in senders)
            or len(senders) != len(set(senders))):
        raise InputError("allowed_senders must be a nonempty unique open_id list")
    groups = config["groups"]
    if (not isinstance(groups, dict)
            or any(not identifier(chat, "oc_") or not isinstance(owner, str) or owner not in lines
                   for chat, owner in groups.items())):
        raise InputError("groups must map chat IDs to registered lines")
    return config


@contextmanager
def ledger_file(path, write=False, create=False):
    flags = os.O_RDWR if write else os.O_RDONLY
    flags |= os.O_NOFOLLOW
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise InputError("outbox must be an owned regular file with mode 0600")
        lock = fcntl.LOCK_EX if write else fcntl.LOCK_SH
        try:
            fcntl.flock(fd, lock | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise InputError("outbox is busy; retain and retry the same event") from error
        with os.fdopen(fd, "r+b" if write else "rb", closefd=False) as stream:
            yield stream
    finally:
        os.close(fd)


def read_entries(stream, config):
    entries = {}
    content = read_limited(stream)
    if content and not content.endswith(b"\n"):
        raise InputError("outbox has an incomplete final record")
    records = [raw for raw in content.splitlines() if raw.strip()]
    if not records:
        raise InputError("outbox header missing; an empty/truncated ledger is not initialized")
    header = decode(records[0])
    if (not isinstance(header, dict)
            or set(header) != {"kind", "schema_version", "generation"}
            or header["kind"] != "outbox_header"
            or type(header["schema_version"]) is not int or header["schema_version"] != 1
            or not isinstance(header["generation"], str)
            or re.fullmatch(r"[0-9a-f]{32}", header["generation"]) is None):
        raise InputError("invalid outbox header")
    for raw in records[1:]:
        entry = decode(raw)
        if (not isinstance(entry, dict)
                or not identifier(entry.get("message_id"), "om_")
                or not identifier(entry.get("chat_id"), "oc_")
                or entry.get("line") not in config["lines"]
                or entry.get("identity") not in ("bot", "user")):
            raise InputError("invalid outbox entry")
        key = entry["message_id"]
        if key in entries and entries[key] != entry:
            raise InputError("conflicting outbox entries")
        entries[key] = entry
    return entries


def init_outbox(path, config):
    try:
        with ledger_file(path, write=True, create=True) as stream:
            header = {"kind": "outbox_header", "schema_version": 1, "generation": uuid.uuid4().hex}
            stream.write((json.dumps(header) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        with ledger_file(path) as stream:
            read_entries(stream, config)


def route(event, config, line, outbox):
    if not isinstance(event, dict):
        raise InputError("event must be a JSON object")
    if event.get("type") != "im.message.receive_v1":
        return None
    if event.get("sender_type") != "user" or event.get("sender_id") not in config["allowed_senders"]:
        return None
    mid, chat = event.get("message_id"), event.get("chat_id")
    if not identifier(mid, "om_") or not identifier(chat, "oc_"):
        raise InputError("event lacks a valid message_id or chat_id")
    if mid in outbox:
        return None  # User-identity sends also return as user events.
    reply, root = event.get("reply_to"), event.get("root_id")
    for ref in (reply, root):
        if ref is not None and not identifier(ref, "om_"):
            raise InputError("invalid reply_to or root_id")
    chat_type = event.get("chat_type")
    owner = None
    if chat_type == "group":
        owner = config["groups"].get(chat)
        reason = "unknown_group"
    elif chat_type == "p2p":
        # A root is not necessarily the direct parent. Do not use it to guess
        # ownership when a more specific, unresolved reply_to is present.
        parent = outbox.get(reply or root)
        if parent and parent["chat_id"] == chat:
            owner = parent["line"]
        reason = "unclaimed_p2p"
    else:
        raise InputError("unsupported chat_type")
    metadata = {"message_id": mid, "chat_id": chat, "chat_type": chat_type,
                "reply_to": reply, "root_id": root, "authorization_granted": False}
    if owner is None:
        return dict(metadata, kind="routing_notice", reason=reason, instruction_candidate=False)
    if owner != line:
        return None
    return dict(metadata, kind="message_candidate", owner_line=owner,
                sender_id=event["sender_id"], message_type=event.get("message_type"),
                create_time=event.get("create_time"), content=event.get("content", ""),
                instruction_candidate=True)


def validate_receipt(receipt, identity):
    if not isinstance(receipt, dict) or receipt.get("ok") is not True:
        raise InputError("send receipt must have ok=true")
    if receipt.get("identity") != identity:
        raise InputError("send receipt identity does not match --identity")
    data = receipt.get("data")
    if (not isinstance(data, dict) or not identifier(data.get("message_id"), "om_")
            or not identifier(data.get("chat_id"), "oc_")):
        raise InputError("send receipt lacks valid message_id/chat_id")
    return data["message_id"], data["chat_id"]


def record_receipt(receipt, identity, config, line, path):
    mid, chat = validate_receipt(receipt, identity)
    entry = {"message_id": mid, "chat_id": chat, "identity": identity, "line": line}
    with ledger_file(path, write=True) as stream:
        entries = read_entries(stream, config)
        if mid in entries:
            if any(entries[mid].get(key) != value for key, value in entry.items()):
                raise InputError("receipt conflicts with existing outbox ownership")
            return dict(kind="outbox_recorded", message_id=mid, new=False,
                        receipt_valid=True, readback_verified=False)
        entry["recorded_at"] = datetime.now(timezone.utc).isoformat()
        encoded = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
        stream.seek(0, os.SEEK_END)
        if stream.tell() + len(encoded) > MAX_BYTES:
            raise InputError("outbox exceeds 1 MiB; archive/rotate it explicitly")
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    return dict(kind="outbox_recorded", message_id=mid, new=True,
                receipt_valid=True, readback_verified=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init-outbox", "check", "route", "record-receipt"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--line", required=True)
    parser.add_argument("--outbox", required=True)
    parser.add_argument("--input", help="Captured NDJSON event or JSON send receipt")
    parser.add_argument("--identity", choices=("bot", "user"))
    args = parser.parse_args()
    try:
        config = load_config(args.config, args.line)
        if args.command == "init-outbox":
            init_outbox(args.outbox, config)
            return 0
        if args.command == "record-receipt":
            if not args.input or not args.identity:
                raise InputError("record-receipt requires --input and --identity")
            result = record_receipt(decode(read_file(args.input)), args.identity,
                                    config, args.line, args.outbox)
        else:
            with ledger_file(args.outbox) as stream:
                entries = read_entries(stream, config)
            if args.command == "check":
                return 0
            if not args.input:
                raise InputError("route requires --input")
            frames = [x for x in read_file(args.input).splitlines() if x.strip()]
            if len(frames) > 1:
                raise InputError("expected at most one event from --max-events 1")
            result = route(decode(frames[0]), config, args.line, entries) if frames else None
        if result is not None:
            if result["kind"] == "routing_notice":
                result["capture_ref"] = str(Path(args.input).resolve())
                print(json.dumps(result, ensure_ascii=False), flush=True)
                return 3  # Deferred ownership: caller must preserve the raw capture.
            print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0
    except (InputError, OSError) as error:
        print("feishu-loop: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
