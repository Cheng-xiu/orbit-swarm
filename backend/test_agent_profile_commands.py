"""Focused checks for chat-box role configuration commands."""

from __future__ import annotations

import unittest

from agent_profile_commands import parse_agent_profile_command
from executors import RuntimeConfig, updated_config


class AgentProfileCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = RuntimeConfig.from_environment()

    def test_keep_only_roles(self) -> None:
        command = parse_agent_profile_command(
            "\u6a21\u5f0f2\u53ea\u4fdd\u7559\u7cfb\u7edf\u67b6\u6784\u5e08\u3001\u540e\u7aef\u5f00\u53d1\u7ec4\u548c\u6d4b\u8bd5\u5f00\u53d1\u7ec4",
            self.config,
        )
        self.assertIsNotNone(command)
        self.assertEqual(
            [item["role"] for item in command.payload["roles"]],
            ["\u7cfb\u7edf\u67b6\u6784\u5e08", "\u540e\u7aef\u5f00\u53d1\u7ec4", "\u6d4b\u8bd5\u5f00\u53d1\u7ec4"],
        )

    def test_model_and_executor_edit(self) -> None:
        command = parse_agent_profile_command(
            "\u628a\u6a21\u5f0f1\u5168\u6808\u5f00\u53d1\u6539\u6210 Codex\uff0c\u6a21\u578b GPT-5.6 Terra",
            self.config,
        )
        self.assertIsNotNone(command)
        updated = updated_config(self.config, command.payload)
        role = next(item for item in updated.agent_profiles["1"] if item["role"] == "\u5168\u6808\u5f00\u53d1")
        self.assertEqual((role["executor"], role["model"]), ("codex", "GPT-5.6 Terra"))

    def test_slot_and_executor_edit(self) -> None:
        command = parse_agent_profile_command(
            "\u6a21\u5f0f3\u6587\u6863\u6267\u884c\u7ec4\u6539\u4e3a5\u4e2a\uff0c\u4f7f\u7528 OpenClaw",
            self.config,
        )
        self.assertIsNotNone(command)
        updated = updated_config(self.config, command.payload)
        role = next(item for item in updated.agent_profiles["3"] if item["role"] == "\u6587\u6863\u6267\u884c\u7ec4")
        self.assertEqual((role["max_count"], role["executor"]), (5, "openclaw"))

    def test_add_and_remove_roles(self) -> None:
        added = parse_agent_profile_command("\u6a21\u5f0f1\u6dfb\u52a0\u89c2\u5bdf\u54582\u4e2a\uff0c\u4f7f\u7528 OpenClaw", self.config)
        self.assertIsNotNone(added)
        configured = updated_config(self.config, added.payload)
        observer = next(item for item in configured.agent_profiles["1"] if item["role"] == "\u89c2\u5bdf\u5458")
        self.assertEqual((observer["max_count"], observer["executor"]), (2, "openclaw"))
        removed = parse_agent_profile_command("\u6a21\u5f0f1\u79fb\u9664\u89c2\u5bdf\u5458", configured)
        self.assertIsNotNone(removed)
        configured = updated_config(configured, removed.payload)
        self.assertNotIn("\u89c2\u5bdf\u5458", [item["role"] for item in configured.agent_profiles["1"]])

    def test_clear_roles_is_explicit(self) -> None:
        command = parse_agent_profile_command("\u6a21\u5f0f2\u6e05\u7a7a\u5c97\u4f4d", self.config)
        self.assertIsNotNone(command)
        updated = updated_config(self.config, command.payload)
        self.assertEqual(updated.agent_profiles["2"], [])

    def test_keep_only_inherits_default_executors(self) -> None:
        command = parse_agent_profile_command("\u6a21\u5f0f1\u53ea\u4fdd\u7559\u5168\u6808\u5f00\u53d1\u548c\u6587\u6863/\u8fd0\u7ef4", self.config)
        self.assertIsNotNone(command)
        executors = {item["role"]: item["executor"] for item in command.payload["roles"]}
        self.assertEqual(executors["\u5168\u6808\u5f00\u53d1"], "codex")
        self.assertEqual(executors["\u6587\u6863/\u8fd0\u7ef4"], "openclaw")


if __name__ == "__main__":
    unittest.main()
