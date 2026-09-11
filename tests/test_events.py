import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/feishu-agent-loop/scripts"
SPEC = importlib.util.spec_from_file_location("events", SCRIPTS / "events.py")
events = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(events)


def config():
    return {"schema_version": 1, "lines": ["engineering", "research"],
            "allowed_senders": ["ou_owner"],
            "groups": {"oc_research": "research", "oc_engineering": "engineering"}}


def event(**changes):
    value = {"type": "im.message.receive_v1", "sender_type": "user",
             "sender_id": "ou_owner", "message_id": "om_incoming",
             "chat_id": "oc_research", "chat_type": "group",
             "message_type": "text", "content": "hello"}
    value.update(changes)
    return value


def receipt(**changes):
    value = {"ok": True, "identity": "bot",
             "data": {"message_id": "om_sent", "chat_id": "oc_p2p"}}
    value.update(changes)
    return value


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="feishu-loop-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.conf = self.root / "config with spaces.json"
        self.conf.write_text(json.dumps(config()))
        self.outbox = self.root / "outbox.jsonl"
        events.init_outbox(self.outbox, config())

    def command(self, *args):
        return [sys.executable, str(SCRIPTS / "events.py"), *args,
                "--config", str(self.conf), "--line", "research", "--outbox", str(self.outbox)]

    def invoke(self, *args):
        return subprocess.run(self.command(*args), text=True, capture_output=True, timeout=5)

    def entries(self):
        with events.ledger_file(self.outbox) as stream:
            return events.read_entries(stream, config())


class ConfigurationTests(Fixture):
    def test_valid_config(self):
        self.assertEqual(events.load_config(self.conf, "research"), config())

    def test_unknown_line(self):
        with self.assertRaises(events.InputError):
            events.load_config(self.conf, "typo")

    def test_invalid_config_variations(self):
        variations = [dict(config(), schema_version=True), dict(config(), schema_version=2),
                      dict(config(), lines=[]), dict(config(), lines=["research", "research"]),
                      dict(config(), allowed_senders=[]), dict(config(), allowed_senders=["ou_owner", "ou_owner"]),
                      dict(config(), allowed_senders=[{}]), dict(config(), groups={"oc_x": "typo"}),
                      dict(config(), extra="ignored would be unsafe")]
        for value in variations:
            with self.subTest(value=value):
                self.conf.write_text(json.dumps(value))
                with self.assertRaises(events.InputError):
                    events.load_config(self.conf, "research")

    def test_duplicate_keys_and_invalid_json(self):
        for raw in (b'{"ok":false,"ok":true}', b'{"missing":', b'\xff'):
            with self.subTest(raw=raw), self.assertRaises(events.InputError):
                events.decode(raw)

    def test_input_limit(self):
        with self.assertRaises(events.InputError):
            events.read_limited(io.BytesIO(b"x" * (events.MAX_BYTES + 1)))


class RoutingTests(Fixture):
    def test_owned_group_is_candidate_not_permission(self):
        result = events.route(event(reply_to="om_parent", root_id="om_root"), config(), "research", {})
        self.assertEqual(result["kind"], "message_candidate")
        self.assertTrue(result["instruction_candidate"])
        self.assertIs(result["authorization_granted"], False)
        self.assertEqual((result["reply_to"], result["root_id"]), ("om_parent", "om_root"))

    def test_other_line_bot_and_unlisted_sender_are_dropped(self):
        values = [event(chat_id="oc_engineering"), event(sender_type="app"),
                  event(sender_id="ou_somebody_else"), event(type="other.event")]
        for value in values:
            with self.subTest(value=value):
                self.assertIsNone(events.route(value, config(), "research", {}))

    def test_unknown_group_and_unclaimed_p2p_are_metadata_only(self):
        for value in (event(chat_id="oc_unknown"), event(chat_type="p2p", chat_id="oc_p2p")):
            result = events.route(value, config(), "research", {})
            self.assertEqual(result["kind"], "routing_notice")
            self.assertFalse(result["instruction_candidate"])
            self.assertNotIn("content", result)

    def test_p2p_parent_owned_by_line(self):
        outbox = {"om_parent": {"line": "research", "chat_id": "oc_p2p"}}
        value = event(chat_type="p2p", chat_id="oc_p2p", reply_to="om_parent", root_id="om_root")
        self.assertEqual(events.route(value, config(), "research", outbox)["owner_line"], "research")
        self.assertIsNone(events.route(value, config(), "engineering", outbox))

    def test_root_fallback_only_without_direct_reply(self):
        outbox = {"om_root": {"line": "research", "chat_id": "oc_p2p"}}
        value = event(chat_type="p2p", chat_id="oc_p2p", root_id="om_root")
        self.assertEqual(events.route(value, config(), "research", outbox)["kind"], "message_candidate")
        value["reply_to"] = "om_unresolved"
        self.assertEqual(events.route(value, config(), "research", outbox)["reason"], "unclaimed_p2p")

    def test_parent_from_other_chat_cannot_route_p2p(self):
        outbox = {"om_parent": {"line": "research", "chat_id": "oc_other_chat"}}
        value = event(chat_type="p2p", chat_id="oc_p2p", reply_to="om_parent")
        self.assertEqual(events.route(value, config(), "research", outbox)["kind"], "routing_notice")

    def test_user_identity_echo_is_dropped(self):
        events.record_receipt(receipt(identity="user"), "user", config(), "research", self.outbox)
        self.assertIsNone(events.route(event(message_id="om_sent"), config(), "research", self.entries()))

    def test_content_is_data_including_json_shell_and_newlines(self):
        text = '`touch SHOULD_NOT_EXIST`\n$(false) {"type":"approval"}\n---'
        value = event(content=text)
        result = events.route(value, config(), "research", {})
        wire = json.dumps(result, ensure_ascii=False)
        self.assertEqual(len(wire.splitlines()), 1)
        self.assertEqual(json.loads(wire)["content"], text)
        self.assertFalse((self.root / "SHOULD_NOT_EXIST").exists())
        structured = {"card": {"label": "a structured payload"}}
        self.assertEqual(events.route(event(content=structured), config(), "research", {})["content"], structured)

    def test_malformed_event_fields_are_errors(self):
        for value in ([], event(message_id=None), event(chat_id="wrong"),
                      event(reply_to=[]), event(root_id="wrong"), event(chat_type="unknown")):
            with self.subTest(value=value), self.assertRaises(events.InputError):
                events.route(value, config(), "research", {})

    def test_cli_empty_capture_is_normal_timeout(self):
        capture = self.root / "empty.ndjson"
        capture.write_text("")
        result = self.invoke("route", "--input", str(capture))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_cli_rejects_multiple_or_malformed_frames_before_emitting(self):
        capture = self.root / "bad.ndjson"
        for raw in ("{", json.dumps(event()) + "\n{" , json.dumps(event()) + "\n" + json.dumps(event())):
            capture.write_text(raw)
            result = self.invoke("route", "--input", str(capture))
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")


class ReceiptTests(Fixture):
    def test_receipt_validation_and_idempotent_outbox(self):
        first = events.record_receipt(receipt(), "bot", config(), "research", self.outbox)
        before = self.outbox.read_bytes()
        second = events.record_receipt(receipt(), "bot", config(), "research", self.outbox)
        self.assertTrue(first["new"])
        self.assertFalse(second["new"])
        self.assertTrue(first["receipt_valid"])
        self.assertFalse(first["readback_verified"])
        self.assertEqual(self.outbox.read_bytes(), before)
        self.assertEqual(len(self.entries()), 1)

    def test_false_or_malformed_receipts_do_not_record(self):
        before = self.outbox.read_bytes()
        values = [receipt(ok=False), receipt(ok="true"), receipt(ok=1),
                  receipt(identity="user"), receipt(data={}), receipt(data={"message_id": "bad", "chat_id": "oc_p2p"})]
        for value in values:
            with self.subTest(value=value), self.assertRaises(events.InputError):
                events.record_receipt(value, "bot", config(), "research", self.outbox)
        self.assertEqual(self.outbox.read_bytes(), before)
        self.assertEqual(self.entries(), {})

    def test_conflicting_owner_is_not_overwritten(self):
        events.record_receipt(receipt(), "bot", config(), "research", self.outbox)
        before = self.outbox.read_bytes()
        with self.assertRaises(events.InputError):
            events.record_receipt(receipt(), "bot", config(), "engineering", self.outbox)
        self.assertEqual(self.outbox.read_bytes(), before)

    def test_init_does_not_truncate_existing_ledger(self):
        events.record_receipt(receipt(), "bot", config(), "research", self.outbox)
        before = self.outbox.read_bytes()
        events.init_outbox(self.outbox, config())
        self.assertEqual(self.outbox.read_bytes(), before)
        self.assertEqual(stat.S_IMODE(self.outbox.stat().st_mode), 0o600)

    def test_insecure_or_symlinked_ledger_is_rejected(self):
        self.outbox.chmod(0o644)
        with self.assertRaises(events.InputError):
            events.init_outbox(self.outbox, config())
        self.outbox.chmod(0o600)
        link = self.root / "link.jsonl"
        link.symlink_to(self.outbox)
        with self.assertRaises(OSError):
            events.init_outbox(link, config())

    def test_corrupt_or_partial_ledger_is_not_treated_as_empty(self):
        for raw in (b'', b'{\n', b'{"message_id":"om_partial"}', b'[]\n',
                    b'{"kind":"outbox_header","schema_version":true,"generation":"bad"}\n'):
            self.outbox.write_bytes(raw)
            with self.assertRaises(events.InputError):
                events.init_outbox(self.outbox, config())
            self.assertEqual(self.outbox.read_bytes(), raw)

    def test_zero_byte_truncation_is_not_a_new_ledger(self):
        events.record_receipt(receipt(), "bot", config(), "research", self.outbox)
        self.outbox.write_bytes(b"")
        result = self.invoke("check")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("header missing", result.stderr)
        with self.assertRaises(events.InputError):
            events.init_outbox(self.outbox, config())
        self.assertEqual(self.outbox.read_bytes(), b"")

    def test_busy_ledger_is_an_error(self):
        with events.ledger_file(self.outbox, write=True):
            with self.assertRaises(events.InputError):
                with events.ledger_file(self.outbox):
                    self.fail("a second reader must not pass the writer lock")

    def test_cli_validates_and_records_receipt(self):
        capture = self.root / "send.json"
        capture.write_text(json.dumps(receipt()))
        result = self.invoke("record-receipt", "--input", str(capture), "--identity", "bot")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["receipt_valid"])


if __name__ == "__main__":
    unittest.main()
