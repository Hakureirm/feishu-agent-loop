import json
import os
from pathlib import Path
import subprocess
import sys
import time

from test_events import Fixture, SCRIPTS, config, event, events, receipt


class ListenerTests(Fixture):
    def setUp(self):
        super().setUp()
        self.calls = self.root / "calls.jsonl"
        self.steps = self.root / "steps.json"
        self.fake = self.root / "fake-lark"
        self.fake.write_text("#!" + sys.executable + "\n" + '''
import json, os, signal, sys, time
from pathlib import Path
calls = Path(os.environ["FAKE_CALLS"])
prior = calls.read_text().splitlines() if calls.exists() else []
with calls.open("a") as f:
    f.write(json.dumps({"args": sys.argv[1:], "pid": os.getpid()}) + "\\n")
steps = json.loads(Path(os.environ["FAKE_STEPS"]).read_text())
step = steps[min(len(prior), len(steps) - 1)]
print("[event] ready synthetic-consumer", file=sys.stderr, flush=True)
if step.get("wait"):
    def stopped(signum, frame):
        Path(os.environ["FAKE_STOPPED"]).write_text("terminated")
        sys.exit(0)
    signal.signal(signal.SIGTERM, stopped)
    Path(os.environ["FAKE_READY"]).write_text("ready")
    time.sleep(30)
print(step.get("stdout", ""), end="", flush=True)
print(step.get("stderr", "[event] exited synthetic-consumer"), file=sys.stderr, flush=True)
sys.exit(step.get("rc", 0))
''')
        self.fake.chmod(0o700)
        self.environment = dict(os.environ, LARK_CLI=str(self.fake), PYTHON3=sys.executable,
                                FEISHU_LOOP_RETRY_DELAY="0", FEISHU_LOOP_MAX_CYCLES="1",
                                FEISHU_LOOP_CONSUME_SECONDS="2", FEISHU_LOOP_MAX_DEFERRED="32",
                                TMPDIR=str(self.root),
                                FAKE_CALLS=str(self.calls), FAKE_STEPS=str(self.steps),
                                FAKE_READY=str(self.root / "ready"),
                                FAKE_STOPPED=str(self.root / "stopped"))

    def listener_command(self, line="research"):
        return ["bash", str(SCRIPTS / "listen.sh"), str(self.conf), line, str(self.outbox)]

    def run_listener(self, steps, max_cycles="1", line="research"):
        self.steps.write_text(json.dumps(steps))
        env = dict(self.environment, FEISHU_LOOP_MAX_CYCLES=max_cycles)
        return subprocess.run(self.listener_command(line), env=env, text=True,
                              capture_output=True, timeout=8)

    def recorded_calls(self):
        return [json.loads(x) for x in self.calls.read_text().splitlines()]

    def test_one_bounded_consume_and_diagnostics_are_preserved(self):
        result = self.run_listener([{"stdout": json.dumps(event()) + "\n"}])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["kind"], "message_candidate")
        self.assertIn("[event] ready", result.stderr)
        args = self.recorded_calls()[0]["args"]
        self.assertEqual(args, ["event", "consume", "im.message.receive_v1", "--as", "bot",
                                "--timeout", "2s", "--max-events", "1"])
        self.assertEqual(list(self.root.glob("feishu-loop.*")), [])

    def test_three_consecutive_empty_failures_stop(self):
        result = self.run_listener([{"rc": 4, "stderr": "synthetic network error"}], "0")
        self.assertEqual(result.returncode, 1)
        output = [json.loads(x) for x in result.stdout.splitlines()]
        self.assertEqual([x["consecutive_failures"] for x in output[:-1]], [1, 2, 3])
        self.assertEqual(output[-1]["kind"], "listener_stopped")
        self.assertEqual(len(self.recorded_calls()), 3)
        self.assertIn("synthetic network error", result.stderr)
        self.assertEqual(list(self.root.glob("feishu-loop.*/event.*.ndjson")), [])

    def test_failed_consume_with_output_cannot_be_cleared_by_later_success(self):
        raw = json.dumps(event()) + "\n"
        result = self.run_listener([{"rc": 4, "stdout": raw}, {}], "2")
        self.assertEqual(result.returncode, 4)
        output = [json.loads(x) for x in result.stdout.splitlines()]
        self.assertEqual(output[-1]["reason"], "consume_failed_with_retained_output")
        self.assertEqual(len(self.recorded_calls()), 1)
        retained = list(self.root.glob("feishu-loop.*/event.*.ndjson"))
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_text(), raw)
        self.assertEqual(retained[0].stat().st_mode & 0o777, 0o600)

    def test_success_resets_consecutive_failures(self):
        result = self.run_listener([{"rc": 4}, {}, {"rc": 4}, {"rc": 4}, {"rc": 4}], "0")
        self.assertEqual(result.returncode, 1)
        output = [json.loads(x) for x in result.stdout.splitlines()]
        self.assertEqual([x["consecutive_failures"] for x in output[:-1]], [1, 1, 2, 3])
        self.assertEqual(len(self.recorded_calls()), 5)

    def test_route_error_stops_before_consuming_another_event(self):
        result = self.run_listener([{"stdout": "{"}, {"stdout": json.dumps(event())}], "0")
        self.assertEqual(result.returncode, 2)
        output = [json.loads(x) for x in result.stdout.splitlines()]
        self.assertEqual(output[0]["stage"], "route")
        self.assertEqual(output[-1]["reason"], "event_retained_for_repair")
        self.assertEqual(len(self.recorded_calls()), 1)
        retained = list(self.root.glob("feishu-loop.*/event.*.ndjson"))
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_text(), "{")

    def test_bad_preflight_never_starts_consumer(self):
        result = self.run_listener([{}], line="unregistered")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["stage"], "preflight")
        self.assertFalse(self.calls.exists())

    def test_empty_timeout_is_not_a_failure(self):
        result = self.run_listener([{}])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_bounded_debug_run_does_not_turn_error_green(self):
        result = self.run_listener([{"rc": 4}])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["stage"], "consume")

    def test_unclaimed_p2p_is_retained_and_can_be_routed_after_repair(self):
        value = event(chat_type="p2p", chat_id="oc_p2p", reply_to="om_parent", content="private synthetic text")
        result = self.run_listener([{"stdout": json.dumps(value) + "\n"}])
        self.assertEqual(result.returncode, 3)
        notice = json.loads(result.stdout)
        self.assertEqual(notice["reason"], "unclaimed_p2p")
        self.assertNotIn("content", notice)
        capture = Path(notice["capture_ref"])
        self.assertEqual(json.loads(capture.read_text()), value)
        self.assertEqual(capture.stat().st_mode & 0o777, 0o600)
        events.record_receipt(receipt(data={"message_id": "om_parent", "chat_id": "oc_p2p"}),
                              "bot", config(), "research", self.outbox)
        replay = self.invoke("route", "--input", str(capture))
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertEqual(json.loads(replay.stdout)["content"], value["content"])
        self.assertTrue(capture.exists())

    def test_later_owned_event_does_not_clear_deferred_capture(self):
        unknown = event(chat_type="p2p", chat_id="oc_p2p")
        result = self.run_listener([{"stdout": json.dumps(unknown) + "\n"},
                                    {"stdout": json.dumps(event()) + "\n"}], "2")
        self.assertEqual(result.returncode, 3)
        output = [json.loads(x) for x in result.stdout.splitlines()]
        self.assertEqual([x["kind"] for x in output], ["routing_notice", "message_candidate"])
        self.assertTrue(Path(output[0]["capture_ref"]).exists())
        self.assertEqual(len(list(self.root.glob("feishu-loop.*/event.*.ndjson"))), 1)

    def test_deferred_capture_count_is_bounded(self):
        self.environment["FEISHU_LOOP_MAX_DEFERRED"] = "2"
        result = self.run_listener([{"stdout": json.dumps(event(chat_id="oc_unknown")) + "\n"}], "0")
        self.assertEqual(result.returncode, 3)
        output = [json.loads(x) for x in result.stdout.splitlines()]
        self.assertEqual(output[-1]["reason"], "deferred_capture_limit")
        self.assertEqual(len(self.recorded_calls()), 2)
        self.assertEqual(len(list(self.root.glob("feishu-loop.*/event.*.ndjson"))), 2)

    def test_sigterm_targets_only_own_consumer_and_retains_capture(self):
        self.steps.write_text(json.dumps([{"wait": True}]))
        proc = subprocess.Popen(self.listener_command(), env=self.environment, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 4
            ready = Path(self.environment["FAKE_READY"])
            while not ready.exists() and time.monotonic() < deadline and proc.poll() is None:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "fake consumer never became ready")
            proc.terminate()
            stdout, stderr = proc.communicate(timeout=4)
            self.assertEqual(proc.returncode, 143, stdout + stderr)
            self.assertTrue(Path(self.environment["FAKE_STOPPED"]).exists())
            self.assertEqual(len(self.recorded_calls()), 1)
            self.assertEqual(len(list(self.root.glob("feishu-loop.*/event.*.ndjson"))), 1)
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.communicate(timeout=4)


if __name__ == "__main__":
    import unittest
    unittest.main()
