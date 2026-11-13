import asyncio
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, AsyncMock

sys.path.insert(0, str(Path(__file__).parent / "scripts"))
import liveasset as la


class Model:
    def __init__(self):
        self.prompts = []
        self.selected = ["evaluation"]
        self.relation = "continue"
        self.invalid = False

    async def chat(self, messages):
        self.prompts.append(messages[0]["content"])
        reply = {"selected": [{"situation_id": s, "reason": "Fixture"} for s in self.selected]}
        if '"bound_task"' in messages[0]["content"]:
            reply["relation"] = "invalid" if self.invalid else self.relation
        return SimpleNamespace(content=json.dumps(reply))


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    def test_api_transport_only_allows_https_or_local_proxy(self):
        for url in ("https://example.com/v1", "http://127.0.0.1:8888/v1", "http://[::1]:8888/v1"):
            self.assertTrue(la.api_endpoint_allowed(url))
        for url in ("http://example.com/v1", "https://", "http://127.0.0.1.example.com/v1", "https://user:password@example.com"):
            self.assertFalse(la.api_endpoint_allowed(url))

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.asset = json.loads((la.ROOT / "cases/assets.json").read_text())
        la.save(self.root / "asset.json", self.asset)
        self.model = Model()
        self.service = la.Service(self.root, self.model)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def start(self, **kw):
        return await self.service.match("运行评估", session_id="a", scope="project", **kw)

    async def test_restart_cross_chat_continuation_and_bound_check_do_not_rematch(self):
        initial = await self.start()
        restarted = la.Service(self.root, self.model)
        result = await restarted.match("继续", session_id="b", task_id=initial["task_id"], scope="project")
        self.assertEqual(result["task_id"], initial["task_id"])
        self.assertEqual(result["calls"], 0)
        self.assertEqual(len(self.model.prompts), 1)
        self.assertIn("运行评估", result["actor_context"])
        with patch.object(la.si.IntentProgress, "check", AsyncMock(return_value={"complete": True, "revise": False})):
            await restarted.check("继续", "结果", task_id=result["task_id"], scope="project")
        self.assertEqual(len(self.model.prompts), 1)

    async def test_short_continuation_reports_the_current_relation(self):
        first = await self.start()
        resumed = await self.service.match("继续", session_id="a", scope="project")
        self.assertEqual(resumed["task_id"], first["task_id"])
        self.assertEqual(resumed["calls"], 0)
        self.assertEqual(resumed["relation"], "continue")
        self.assertEqual(resumed["routing"]["relation"], "continue")

    async def test_bound_check_retains_correction_and_observed_materials(self):
        first = await self.start()
        context = {"tool_observations": [{"tool": "read_file", "output": "当前批次只包含24题。"}]}
        await self.service.match("预算改为24题", context, session_id="a", scope="project", reroute=True)
        resumed = await self.service.match("继续", session_id="b", task_id=first["task_id"], scope="project")
        seen = []

        class Checker:
            async def chat(self, messages):
                payload = json.loads(messages[0]["content"].split("INPUT:\n", 1)[1])
                seen.append(payload)
                return SimpleNamespace(content=json.dumps({"nodes": [
                    {"intent_id": node["intent_id"], "items": [
                        {"id": item["id"], "status": "met", "reason": "fixture"}
                        for item in node["requirements"] + node.get("facts", [])]}
                    for node in payload["nodes"]]}))

        checked = await la.Service(self.root, Checker()).check(
            "继续", "已核对24题", task_id=resumed["task_id"], scope="project")
        self.assertTrue(checked["complete"])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["task_context"], resumed["task_context"])
        self.assertEqual(seen[0]["task_context"]["latest_request"], "预算改为24题")
        self.assertEqual(seen[0]["task_context"]["context"], context)

    async def test_context_change_forces_matching_even_on_continue(self):
        first = await self.start(context={"stage": "run"})
        result = await self.service.match("继续", session_id="a", scope="project", context={"stage": "score"})
        self.assertEqual(result["task_id"], first["task_id"])
        self.assertEqual(result["routing"]["reason"], "context_changed")
        self.assertEqual(len(self.model.prompts), 2)

    async def test_forced_route_and_latest_correction_are_retained(self):
        first = await self.start()
        corrected = await self.service.match("只考虑40题", session_id="a", scope="project", reroute=True)
        self.assertEqual(corrected["routing"]["reason"], "requested")
        resumed = await self.service.match("继续", session_id="b", task_id=first["task_id"], scope="project")
        self.assertIn("只考虑40题", resumed["actor_context"])
        self.assertEqual(len(self.model.prompts), 2)

    async def test_same_chat_new_goal_gets_another_task_and_can_select_nothing(self):
        first = await self.start()
        self.model.relation, self.model.selected = "new", []
        other = await self.service.match("把hello翻译成中文", session_id="a", scope="project")
        self.assertNotEqual(other["task_id"], first["task_id"])
        self.assertEqual(other["selected_ids"], [])
        self.assertEqual(other["calls"], 1)  # Empty is a valid route, not a reason to ask twice.
        state = la.read(self.root / "routing.json")
        self.assertEqual(len(state["sessions"][la.digest(["project", "a"])]["task_ids"]), 2)

    async def test_explicit_task_id_survives_model_new_and_updates_selection(self):
        first = await self.start()
        self.model.relation, self.model.selected = "new", ["repository-layout"]
        result = await self.service.match("把结果目录也整理一下", session_id="b", scope="project",
                                          task_id=first["task_id"], reroute=True)
        self.assertEqual(result["task_id"], first["task_id"])
        self.assertEqual(result["selected_ids"], ["repository-cohesion"])
        self.assertEqual(result["relation"], "continue")
        self.assertEqual(result["routing"]["relation"], "continue")
        self.assertEqual(result["task_context"]["request"], "运行评估")

    async def test_tool_hook_updates_selection_and_stop_checks_latest_snapshot(self):
        base = {"session_id": "hook-tool", "cwd": "project", "turn_id": "one"}
        state_path = self.root / "sessions" / la.digest("hook-tool") / "state.json"
        with patch.object(la, "CodexModel", return_value=self.model):
            await la.hook({**base, "hook_event_name": "UserPromptSubmit", "prompt": "处理这批评估"}, self.root)
            first = la.read(state_path)["selection"]
            self.model.selected, self.model.relation = ["evaluation", "repository-layout"], "new"
            event = {**base, "hook_event_name": "PostToolUse", "tool_use_id": "read-1", "tool_name": "Bash",
                     "tool_input": {"command": "cat handoff.md"}, "tool_response": "验收还要求将结果归回各自代码仓库。"}
            result = await la.hook(event, self.root)
            self.assertIn("repository", json.dumps(la.read(state_path)["selection"]))
            self.assertIn("实际代码仓库", result["hookSpecificOutput"]["additionalContext"])
            self.assertEqual(la.read(state_path)["selection"]["task_id"], first["task_id"])
            calls = len(self.model.prompts)
            self.assertEqual(await la.hook(event, self.root), {})
            self.assertEqual(len(self.model.prompts), calls)
            with patch.object(la.Service, "check", AsyncMock(return_value={"complete": True, "revise": False, "met_items": 4})) as check:
                await la.hook({**base, "hook_event_name": "Stop", "last_assistant_message": "结果"}, self.root)
                self.assertEqual(set(check.call_args.kwargs["selection"]["selected_ids"]),
                                 {"evaluation-evidence", "repository-cohesion"})
            await la.hook({**base, "turn_id": "two", "hook_event_name": "UserPromptSubmit", "prompt": "继续"}, self.root)
            self.assertEqual(la.read(state_path)["selection"]["task_id"], first["task_id"])
            before = state_path.read_bytes()
            self.assertEqual(await la.hook(event, self.root), {})
            self.assertEqual(before, state_path.read_bytes())

    async def test_reference_needs_semantic_continuation_not_just_a_link(self):
        first = await self.start()
        self.model.relation, self.model.selected = "new", ["weekly-report"]
        other = await self.service.match("参考codex://threads/a另写本周周报", session_id="b", scope="project")
        self.assertNotEqual(other["task_id"], first["task_id"])
        self.assertIn('"relationship": "referenced_session"', self.model.prompts[-1])

    async def test_ambiguous_reference_never_chooses_latest_task(self):
        first = await self.start()
        self.model.relation = "new"
        second = await self.service.match("另一个任务", session_id="a", scope="project")
        third = await self.service.match("继续codex://threads/a", session_id="b", scope="project")
        self.assertNotIn(third["task_id"], [first["task_id"], second["task_id"]])
        self.assertEqual(third["routing"]["source"], "unresolved_reference")
        self.assertNotIn('"bound_task"', self.model.prompts[-1])

    async def test_scope_isolation_and_unknown_task_are_rejected(self):
        first = await self.start()
        for kwargs in [{"task_id": first["task_id"], "scope": "another"}, {"task_id": "missing", "scope": "project"}]:
            with self.assertRaises(ValueError):
                await self.service.match("继续", **kwargs)
        with self.assertRaises(ValueError):
            await self.service.check("继续", "结果", task_id=first["task_id"], scope="another")
        self.assertEqual(len(self.model.prompts), 1)

    async def test_content_digest_invalidates_even_without_revision_increment(self):
        await self.start()
        changed = copy.deepcopy(self.asset)
        changed["situations"][0]["intents"][0]["requirements"].append("Changed requirement")
        la.save(self.root / "asset.json", changed)
        result = await self.service.match("继续", session_id="a", scope="project")
        self.assertEqual(result["routing"]["reason"], "asset_changed")
        self.assertEqual(len(self.model.prompts), 2)

    async def test_historical_replay_pins_old_snapshot_and_does_not_mutate_live_state(self):
        first = await self.start(event_id="old")
        changed = copy.deepcopy(self.asset)
        changed["revision"] = 2
        la.save(self.root / "asset.json", changed)
        await self.service.match("继续", session_id="a", scope="project", event_id="new")
        before = (self.root / "routing.json").read_bytes()
        replay = await self.service.match("继续", session_id="a", task_id=first["task_id"], scope="project", replay=True, event_id="old")
        self.assertEqual(replay["asset"]["revision"], 1)
        self.assertEqual(before, (self.root / "routing.json").read_bytes())
        duplicate = await self.start(event_id="old")
        self.assertTrue(duplicate["duplicate_event"])
        self.assertEqual(before, (self.root / "routing.json").read_bytes())
        with self.assertRaises(ValueError):
            await self.service.match("different input", session_id="a", scope="project", event_id="old")

    async def test_invalid_route_preserves_binding_and_assets(self):
        await self.start()
        before = (self.root / "routing.json").read_bytes()
        asset_before = (self.root / "asset.json").read_bytes()
        self.model.invalid = True
        with self.assertRaises(RuntimeError):
            await self.service.match("新的实质消息", session_id="a", scope="project")
        self.assertEqual(before, (self.root / "routing.json").read_bytes())
        self.assertEqual(asset_before, (self.root / "asset.json").read_bytes())

    async def test_unbound_check_does_not_certify_an_invalid_match(self):
        before = (self.root / "asset.json").read_bytes()
        invalid = AsyncMock()
        invalid.chat.return_value = SimpleNamespace(content='{"unexpected":true}')
        with self.assertRaisesRegex(RuntimeError, "delivery was not verified"):
            await la.Service(self.root, invalid).check("运行评估", "已完成")
        self.assertFalse((self.root / "checks").exists())
        self.assertEqual(before, (self.root / "asset.json").read_bytes())
        self.model.selected = []
        checked = await self.service.check("把hello翻译成中文", "你好")
        self.assertTrue(checked["complete"])
        self.assertEqual(checked["selected_ids"], [])

    async def test_concurrent_duplicate_event_routes_once(self):
        a, b = await asyncio.gather(self.start(event_id="one"), self.start(event_id="one"))
        self.assertEqual(a["task_id"], b["task_id"])
        self.assertEqual(len(self.model.prompts), 1)
        self.assertEqual(sum(bool(v.get("duplicate_event")) for v in [a, b]), 1)

    async def test_hook_old_event_replay_does_not_roll_back_current_turn(self):
        base = {"hook_event_name": "UserPromptSubmit", "session_id": "hook-a", "cwd": "project"}
        with patch.object(la, "CodexModel", return_value=self.model):
            await la.hook({**base, "turn_id": "one", "prompt": "运行评估"}, self.root)
            await la.hook({**base, "turn_id": "two", "prompt": "继续"}, self.root)
            state_path = self.root / "sessions" / la.digest("hook-a") / "state.json"
            before = state_path.read_bytes()
            result = await la.hook({**base, "turn_id": "one", "prompt": "运行评估"}, self.root)
            self.assertEqual(result, {})
            self.assertEqual(before, state_path.read_bytes())
        self.assertEqual(len(self.model.prompts), 1)


if __name__ == "__main__":
    unittest.main()
