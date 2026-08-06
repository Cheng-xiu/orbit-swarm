"""HTTP-level regression for editable roles and chat-box commands."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from urllib.request import Request, urlopen

import server_stdlib


class AgentProfileHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory(prefix="orbit-profile-http-")
        server_stdlib.load_persisted_state(Path(self.temp.name))
        self.server = server_stdlib.ThreadingHTTPServer(("127.0.0.1", 0), server_stdlib.Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        server_stdlib.CLUSTER.stop()
        self.temp.cleanup()

    def request(self, path: str, method: str = "GET", payload: dict | None = None) -> dict:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_role_configuration_flow_and_restart(self) -> None:
        initial = self.request("/api/system")
        self.assertEqual(initial["mode"], 0)
        self.assertEqual(initial["cluster"]["expected_slots"], 1)
        self.assertTrue(initial["agent_slots"][0]["agent_name"])

        keep = self.request(
            "/api/tasks",
            "POST",
            {"prompt": "模式2只保留系统架构师、后端开发组和测试开发组"},
        )
        self.assertTrue(keep["configuration_updated"])
        self.assertEqual(keep["status"], "complete")

        profiles = self.request("/api/agent-profiles")["agent_profiles"]["2"]
        self.assertEqual([item["role"] for item in profiles], ["系统架构师", "后端开发组", "测试开发组"])
        self.assertEqual(next(item for item in profiles if item["role"] == "后端开发组")["executor"], "codex")

        added = self.request(
            "/api/tasks",
            "POST",
            {"prompt": "模式2添加文档执行组2个，使用 OpenClaw，模型 DeepSeek V4 Flash，接口 deepseek"},
        )
        self.assertTrue(added["configuration_updated"])
        profiles = self.request("/api/agent-profiles")["agent_profiles"]["2"]
        docs = next(item for item in profiles if item["role"] == "文档执行组")
        self.assertEqual((docs["max_count"], docs["executor"], docs["provider_id"]), (2, "openclaw", "deepseek"))

        self.request("/api/cluster/mode", "POST", {"mode": 2})
        system = self.request("/api/system")
        self.assertEqual(system["cluster"]["expected_slots"], 9)
        self.assertTrue(all(item.get("agent_name") for item in system["agent_slots"]))
        backend = next(item for item in system["agent_slots"] if item["role"] == "后端开发组")
        self.assertEqual(backend["executor"], "codex")

        cleared = self.request("/api/tasks", "POST", {"prompt": "模式2清空岗位"})
        self.assertTrue(cleared["configuration_updated"])
        system = self.request("/api/system")
        self.assertEqual(system["cluster"]["expected_slots"], 0)
        self.assertEqual(system["health"], "degraded")

        blocked = self.request("/api/tasks", "POST", {"prompt": "请分析登录模块"})
        self.assertEqual(blocked["blocked_reason"], "no_configured_roles")
        self.assertEqual(blocked["status"], "complete")

        # Reload the same state directory to exercise the real restart path.
        server_stdlib.CLUSTER.stop()
        server_stdlib.load_persisted_state(Path(self.temp.name))
        restored = server_stdlib.system_payload()
        self.assertEqual(restored["mode"], 2)
        self.assertEqual(restored["agent_profiles"]["2"], [])
        self.assertEqual(restored["cluster"]["expected_slots"], 0)


if __name__ == "__main__":
    unittest.main()
