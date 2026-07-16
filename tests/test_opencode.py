from __future__ import annotations

import json
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from mdreader.services.opencode import OpenCodeError, OpenCodeGateway


class OpenCodeGatewayTests(unittest.TestCase):
    def test_model_validation_only_accepts_opencode_free_models(self) -> None:
        self.assertTrue(OpenCodeGateway.is_free_model("opencode/deepseek-v4-flash-free"))
        self.assertTrue(OpenCodeGateway.is_free_model("opencode/big-pickle"))
        self.assertFalse(OpenCodeGateway.is_free_model("anthropic/claude"))
        self.assertFalse(OpenCodeGateway.is_free_model("opencode/paid-model"))
        self.assertEqual(
            OpenCodeGateway.normalize_model("anthropic/claude"),
            OpenCodeGateway.DEFAULT_MODEL,
        )

    def test_available_models_filters_and_deduplicates_cli_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "opencode"
            executable.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' "
                "'opencode/deepseek-v4-flash-free' "
                "'opencode/deepseek-v4-flash-free' "
                "'opencode/big-pickle' "
                "'opencode/paid-model' "
                "'other/provider-free'\n",
                encoding="utf-8",
            )
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
            gateway = OpenCodeGateway(root, executable=str(executable))
            self.addCleanup(gateway.close)

            models = gateway.available_models()

        self.assertEqual(
            models,
            ("opencode/deepseek-v4-flash-free", "opencode/big-pickle"),
        )

    def test_changing_model_starts_a_new_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gateway = OpenCodeGateway(Path(temporary), executable="/usr/bin/opencode")
            self.addCleanup(gateway.close)
            gateway.session_id = "ses_existing"

            gateway.set_model("opencode/hy3-free")

        self.assertEqual(gateway.model, "opencode/hy3-free")
        self.assertEqual(gateway.session_id, "")

    def test_changing_model_while_response_is_running_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gateway = OpenCodeGateway(Path(temporary), executable="/usr/bin/opencode")
            self.addCleanup(gateway.close)
            with gateway._lock:
                gateway._active = True

            with self.assertRaises(OpenCodeError):
                gateway.set_model("opencode/hy3-free")

    def test_stream_parses_text_and_remembers_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "fake-opencode"
            executable.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' "
                "'{\"type\":\"step_start\",\"sessionID\":\"ses_test\",\"part\":{}}' "
                "'{\"type\":\"text\",\"sessionID\":\"ses_test\",\"part\":{\"text\":\"Hello \"}}' "
                "'{\"type\":\"text\",\"sessionID\":\"ses_test\",\"part\":{\"text\":\"reader\"}}' "
                "'{\"type\":\"step_finish\",\"sessionID\":\"ses_test\",\"part\":{\"tokens\":{\"total\":3}}}'\n",
                encoding="utf-8",
            )
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
            gateway = OpenCodeGateway(root, executable=str(executable))
            self.addCleanup(gateway.close)
            text: list[str] = []
            done: list[dict] = []
            errors: list[Exception] = []

            with patch("mdreader.services.opencode.GLib.idle_add", side_effect=lambda callback, value: callback(value)):
                gateway._stream(
                    [str(executable)],
                    text.append,
                    done.append,
                    errors.append,
                )

        self.assertEqual("".join(text), "Hello reader")
        self.assertEqual(gateway.session_id, "ses_test")
        self.assertEqual(done[0]["type"], "step_finish")
        self.assertEqual(errors, [])

    def test_send_includes_existing_session_without_auto_permission(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "opencode"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
            gateway = OpenCodeGateway(root, executable=str(executable))
            self.addCleanup(gateway.close)
            gateway.session_id = "ses_existing"
            captured: list[list[str]] = []

            def capture(command, *_args):
                captured.append(command)

            with patch.object(gateway, "_stream", side_effect=capture):
                gateway.send("question", on_text=lambda _text: None, on_done=lambda _event: None, on_error=lambda _error: None)
                for _ in range(50):
                    if captured:
                        break
                    threading.Event().wait(0.01)

        self.assertTrue(captured)
        command = captured[0]
        self.assertIn("--session", command)
        self.assertIn("ses_existing", command)
        self.assertNotIn("--auto", command)
        self.assertEqual(command[-1], "question")
        runtime_directory = Path(command[command.index("--dir") + 1])
        self.assertNotEqual(runtime_directory, root)
        self.assertTrue(runtime_directory.name.startswith("mdreader-opencode-"))

    def test_send_is_busy_before_subprocess_starts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "opencode"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
            gateway = OpenCodeGateway(root, executable=str(executable))
            self.addCleanup(gateway.close)
            release = threading.Event()

            with patch.object(gateway, "_stream", side_effect=lambda *_args: release.wait(1)):
                gateway.send(
                    "first",
                    on_text=lambda _text: None,
                    on_done=lambda _event: None,
                    on_error=lambda _error: None,
                )
                self.assertTrue(gateway.running)
                with self.assertRaises(OpenCodeError):
                    gateway.send(
                        "second",
                        on_text=lambda _text: None,
                        on_done=lambda _event: None,
                        on_error=lambda _error: None,
                    )
                release.set()

    def test_immediate_cancel_cannot_miss_process_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "opencode"
            executable.write_text("#!/bin/sh\nexec sleep 5\n", encoding="utf-8")
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
            gateway = OpenCodeGateway(root, executable=str(executable))
            self.addCleanup(gateway.close)
            finished = threading.Event()
            errors: list[Exception] = []

            def on_error(error: Exception) -> None:
                errors.append(error)
                finished.set()

            with patch(
                "mdreader.services.opencode.GLib.idle_add",
                side_effect=lambda callback, value: callback(value),
            ):
                gateway.send(
                    "question",
                    on_text=lambda _text: None,
                    on_done=lambda _event: finished.set(),
                    on_error=on_error,
                )
                gateway.cancel()
                self.assertTrue(finished.wait(2))

            self.assertEqual(len(errors), 1)
            self.assertIn("cancelled", str(errors[0]).lower())
            self.assertFalse(gateway.running)

    def test_subprocess_environment_isolates_agent_from_workspace_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gateway = OpenCodeGateway(Path(temporary), executable="/usr/bin/opencode")
            self.addCleanup(gateway.close)
            environment = gateway._subprocess_environment()

        config = json.loads(environment["OPENCODE_CONFIG_CONTENT"])
        agent = config["agent"]["md-reader"]
        self.assertEqual(agent["permission"], {"*": "deny"})
        self.assertIn("read-only discussion assistant", agent["prompt"])
        self.assertEqual(environment["OPENCODE_PERMISSION"], '{"*": "deny"}')
        self.assertEqual(environment["OPENCODE_DISABLE_PROJECT_CONFIG"], "1")
        self.assertEqual(environment["OPENCODE_DISABLE_EXTERNAL_SKILLS"], "1")
        self.assertEqual(environment["OPENCODE_DISABLE_DEFAULT_PLUGINS"], "1")


if __name__ == "__main__":
    unittest.main()
