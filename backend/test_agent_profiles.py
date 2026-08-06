"""Focused regression checks for user-editable cluster profiles."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cluster import ClusterRuntime, configured_role_specs, role_catalog, role_specs
from executors import RuntimeConfig, persistable_config, resolve_route, restore_persisted_config, run_claude_code, run_codex, updated_config


class AgentProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = RuntimeConfig.from_environment()

    def test_default_executor_matrix(self) -> None:
        roles = {item.role: item for item in role_specs(1)}
        self.assertEqual(roles["全栈开发"].executor, "codex")
        self.assertEqual(roles["文档/运维"].executor, "openclaw")
        self.assertEqual(roles["总管理（GM）"].executor, "direct_model")
        runtime = ClusterRuntime(1, config=self.config, simulation=True)
        fullstack = next(item for item in runtime.task_agents() if item["role"] == "全栈开发")
        self.assertEqual(fullstack["model_name"], self.config.models["medium"])

    def test_route_keys_are_unique(self) -> None:
        keys = [item["role_key"] for item in role_catalog()]
        self.assertEqual(len(keys), len(set(keys)))

    def test_explicit_empty_profile_stays_empty(self) -> None:
        configured = updated_config(self.config, {"agent_profiles": {"2": []}})
        self.assertEqual(configured_role_specs(2, configured), ())
        runtime = ClusterRuntime(2, config=configured, simulation=True)
        status = runtime.status()
        self.assertEqual(status["expected_slots"], 0)
        self.assertEqual(status["role_status"], {})
        self.assertEqual(status["health"], "degraded")

        restored = restore_persisted_config(self.config, persistable_config(configured))
        self.assertEqual(restored.agent_profiles["2"], [])

    def test_custom_profile_and_names_are_stable(self) -> None:
        payload = {
            "profile_mode": 2,
            "roles": [
                {"role": "系统架构师", "max_count": 1},
                {"role": "后端开发组", "max_count": 2, "executor": "codex"},
            ],
        }
        configured = updated_config(self.config, payload)
        first = ClusterRuntime(2, config=configured, simulation=True)
        second = ClusterRuntime(2, config=configured, simulation=True)
        first_agents = first.task_agents("task-a")
        second_agents = second.task_agents("task-b")
        self.assertEqual(len(first_agents), 3)
        self.assertEqual([item["name"] for item in first_agents], [item["name"] for item in second_agents])
        self.assertTrue(all(item["name"] for item in first_agents))
        self.assertEqual(first.status()["role_status"]["后端开发组"]["max"], 2)

        extreme = ClusterRuntime(3, config=self.config, simulation=True)
        extreme_names = [item["name"] for item in extreme.task_agents()]
        self.assertEqual(len(extreme_names), len(set(extreme_names)))

    def test_codex_receives_task_scoped_provider_definition(self) -> None:
        route = resolve_route(self.config, "medium", model_id=self.config.models["medium"])
        self.assertIsNotNone(route)
        captured: list[str] = []

        def fake_run(command, **_kwargs):
            captured.extend(command)
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text("ok", encoding="utf-8")
            return SimpleNamespace(returncode=0)

        with patch("executors.shutil.which", return_value="codex.cmd"), patch("executors.subprocess.run", side_effect=fake_run):
            self.assertEqual(run_codex("test", "medium", self.config, resolved_route=route), "ok")
        self.assertTrue(any(".wire_api=\"responses\"" in item for item in captured))
        self.assertIn(f"model_provider=\"{route.provider_id}\"", captured)
        self.assertEqual(captured[captured.index("--model") + 1], route.model_id)

    def test_claude_code_rejects_non_anthropic_route(self) -> None:
        route = resolve_route(self.config, "medium", model_id=self.config.models["medium"])
        with patch("executors.shutil.which", return_value="claude.exe"):
            with self.assertRaisesRegex(RuntimeError, "Anthropic Messages"):
                run_claude_code("test", "medium", self.config, resolved_route=route)


if __name__ == "__main__":
    unittest.main()
