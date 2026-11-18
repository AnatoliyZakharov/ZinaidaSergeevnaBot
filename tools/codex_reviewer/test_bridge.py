import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import bridge


SYNTHETIC_PR = "https://github.com/AnatoliyZakharov/ZinaidaSergeevnaBot/pull/999999999"
HEAD = "1" * 40
OTHER_HEAD = "2" * 40
MARKER = "SYNTHETIC_PRIVATE_MARKER"


class BridgeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.state_dir = self.directory / "state"
        self.command = [sys.executable, str(Path(bridge.__file__).resolve()),
                        "--state-dir", str(self.state_dir)]

    def invoke(self, *args, payload=b""):
        return subprocess.run([*self.command, *args], input=payload, capture_output=True,
                              cwd=self.directory, start_new_session=True, timeout=5)

    def success(self, *args, payload=b""):
        result = self.invoke(*args, payload=payload)
        self.assertEqual((result.returncode, result.stderr), (0, b""))
        return json.loads(result.stdout)

    def failed(self, *args, payload=b"", code=1):
        result = self.invoke(*args, payload=payload)
        self.assertEqual((result.returncode, result.stdout), (code, b""))
        self.assertTrue(result.stderr)
        self.assertNotIn(MARKER.encode(), result.stderr)
        return result.stderr

    def request(self):
        request = self.success("request", SYNTHETIC_PR, HEAD)
        self.assertRegex(request["request_id"], "^[0-9a-f]{32}$")
        self.assertEqual(request, {"request_id": request["request_id"], "pr_url": SYNTHETIC_PR, "head_sha": HEAD})
        return request

    def reply(self, request_id, verdict="PASS"):
        return {"request_id": request_id, "head_sha": HEAD, "verdict": verdict, "findings": ["Synthetic answer ✓"]}

    def test_exchange_and_recovery_in_separate_sessions(self):
        self.assertEqual(self.success("status"), [])
        request = self.request()
        identity = request["request_id"]
        pending = dict(request, status="pending")
        self.assertEqual(self.success("status"), [pending])
        self.assertEqual(self.success("status", identity), pending)
        self.assertIn(b"pending", self.failed("result", identity, HEAD))
        reply = self.reply(identity)
        self.assertEqual(self.success("import", identity, HEAD, payload=bridge.encode(reply)),
                         dict(request, status="received"))
        self.assertEqual(self.success("status"), [dict(request, status="received")])
        self.assertEqual(self.success("result", identity, HEAD), reply)
        self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.state_dir / f"{identity}.json").stat().st_mode), 0o600)

    def test_invalid_arguments_and_untrusted_payloads(self):
        self.failed("--" + MARKER, code=2)
        self.failed("request", SYNTHETIC_PR + "?" + MARKER, HEAD)
        self.failed("request", SYNTHETIC_PR, MARKER)
        self.failed("status", "../../" + MARKER)
        identity = self.request()["request_id"]
        reply = self.reply(identity)
        for bad in (
            MARKER.encode(),
            bridge.encode(dict(reply, request_id="a" * 32)),
            bridge.encode(dict(reply, head_sha=OTHER_HEAD)),
            bridge.encode(dict(reply, authorization=MARKER)),
            bridge.encode(dict(reply, verdict="ALLOW")),
            bridge.encode(dict(reply, findings=[MARKER + "\x1b"])),
            bridge.encode(dict(reply, findings=["x" * 1001])),
            json.dumps(dict(reply, findings=["\ud800"])).encode(),
            bridge.encode(dict(reply, findings=["x"] * 21)),
            b'{"verdict":"PASS",' + bridge.encode(reply)[1:],
            b" " * (bridge.MAX_REPLY_BYTES + 1),
        ):
            with self.subTest(size=len(bad)):
                self.failed("import", identity, HEAD, payload=bad)
        self.assertEqual(self.success("status", identity)["status"], "pending")

    def test_stale_and_conflicting_replies(self):
        identity = self.request()["request_id"]
        reply = self.reply(identity)
        stale = dict(reply, head_sha=OTHER_HEAD)
        self.assertIn(b"stale", self.failed("import", identity, OTHER_HEAD, payload=bridge.encode(stale)))
        original = self.success("import", identity, HEAD, payload=bridge.encode(reply))
        self.assertEqual(self.success("import", identity, HEAD, payload=bridge.encode(reply)), original)
        self.assertIn(b"already recorded", self.failed("import", identity, HEAD,
                                                      payload=bridge.encode(self.reply(identity, "FAIL"))))
        self.assertIn(b"stale", self.failed("result", identity, OTHER_HEAD))
        self.assertEqual(self.success("result", identity, HEAD), reply)

    def test_interrupted_write_preserves_pending(self):
        identity = self.request()["request_id"]
        with bridge.open_state(self.state_dir) as state:
            record = state.read(identity)
            with patch.object(os, "replace", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    state.write(dict(record, reply=self.reply(identity)))
        self.assertEqual(self.success("status", identity)["status"], "pending")
        self.assertEqual(len(list(self.state_dir.iterdir())), 1)
        self.assertEqual(self.success("import", identity, HEAD,
                                      payload=bridge.encode(self.reply(identity)))["status"], "received")

    def test_abrupt_exit_leaves_only_recoverable_pending_state(self):
        identity = self.request()["request_id"]
        crash_before_publish = """
import os
from pathlib import Path
import sys
import bridge

def stop(*args, **kwargs):
    os._exit(124)

with bridge.open_state(Path(sys.argv[1])) as state:
    record = state.read(sys.argv[2])
    os.replace = stop
    state.write(dict(record, reply={"request_id": sys.argv[2], "head_sha": record["head_sha"],
                                    "verdict": "PASS", "findings": ["Synthetic response"]}))
"""
        process = subprocess.run([sys.executable, "-c", crash_before_publish, str(self.state_dir), identity],
                                 capture_output=True, cwd=Path(bridge.__file__).parent, timeout=5)
        self.assertEqual((process.returncode, process.stdout, process.stderr), (124, b"", b""))
        self.assertEqual(len(list(self.state_dir.glob(".tmp-*"))), 1)
        self.assertEqual(self.success("status"), [
            {"request_id": identity, "head_sha": HEAD, "pr_url": SYNTHETIC_PR, "status": "pending"},
        ])
        self.assertEqual(self.success("import", identity, HEAD,
                                      payload=bridge.encode(self.reply(identity)))["status"], "received")

    def test_contending_imports_keep_a_single_reply(self):
        identity = self.request()["request_id"]
        command = [*self.command, "import", identity, HEAD]
        processes = []
        for verdict in ("PASS", "FAIL"):
            payload = bridge.encode(self.reply(identity, verdict))
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, cwd=self.directory)
            self.addCleanup(process.wait, timeout=5)
            processes.append((verdict, process, payload))
        winner = None
        errors = 0
        for verdict, process, payload in processes:
            stdout, stderr = process.communicate(payload, timeout=5)
            if process.returncode == 0:
                self.assertEqual(json.loads(stdout)["status"], "received")
                winner = verdict
            else:
                self.assertEqual((process.returncode, stdout), (1, b""))
                self.assertIn(b"already recorded", stderr)
                errors += 1
        self.assertEqual(errors, 1)
        self.assertEqual(self.success("result", identity, HEAD)["verdict"], winner)

    def test_insecure_state_is_rejected(self):
        self.state_dir.mkdir(mode=0o755)
        self.failed("request", SYNTHETIC_PR, HEAD)
        self.state_dir.chmod(0o700)
        identity = self.request()["request_id"]
        (self.state_dir / f"{identity}.json").chmod(0o644)
        self.failed("status", identity)

    def test_git_checkout_and_symlink_are_rejected(self):
        checkout = self.directory / "checkout"
        checkout.mkdir()
        (checkout / ".git").mkdir()
        with self.assertRaises(bridge.ProtocolError):
            with bridge.open_state(checkout / "state", create=True):
                self.fail("state opened inside Git")
        actual = self.directory / "actual-state"
        actual.mkdir(mode=0o700)
        self.state_dir.symlink_to(actual, target_is_directory=True)
        self.failed("request", SYNTHETIC_PR, HEAD)

    def test_legacy_hook_is_inert(self):
        result = subprocess.run([sys.executable, str(Path(bridge.__file__).resolve()), "hook"],
                                input=MARKER.encode(), capture_output=True, timeout=5)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))


if __name__ == "__main__":
    unittest.main()
