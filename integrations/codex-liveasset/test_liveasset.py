import asyncio
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch, AsyncMock

sys.path.insert(0, str(Path(__file__).parent / "scripts"))
import liveasset as la


class AdapterTests(unittest.TestCase):
    def test_learning_without_explicit_request_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = la.Service(tmp)
            with patch.object(la.si, "extract", AsyncMock()) as learner:
                with self.assertRaises(PermissionError):
                    asyncio.run(service.learn([{"role": "user", "content": "learnable correction"}]))
                learner.assert_not_called()
            self.assertFalse((Path(tmp) / "asset.json").exists())

    def test_hooks_neither_learn_nor_resume_old_pending_learning(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "trace.jsonl"
            rows = [{"role": "user", "type": "message", "content": "Write a report"},
                    {"role": "assistant", "type": "message", "phase": "final_answer", "content": "report"}]
            source.write_text("\n".join(json.dumps({"type": "response_item", "payload": x}) for x in rows))
            session = Path(tmp) / "sessions" / la.digest("s")
            pending = session / "pending-learning.json"
            la.save(pending, {"trace": [{"role": "user", "content": "old pending correction"}], "episode": "old"})
            event = {"hook_event_name": "UserPromptSubmit", "session_id": "s", "turn_id": "t",
                     "prompt": "report", "transcript_path": str(source)}
            with patch.object(la.Service, "learn", AsyncMock()) as learner:
                asyncio.run(la.hook(event, tmp))
                asyncio.run(la.hook({**event, "hook_event_name": "Stop", "last_assistant_message": "report"}, tmp))
                learner.assert_not_called()
            self.assertTrue(pending.exists())
            self.assertFalse((Path(tmp) / "asset.json").exists())

    def test_hook_registration_preserves_other_hooks_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.json"
            existing = [{"hooks": [{"type": "command", "command": "existing"}]}]
            path.write_text(json.dumps({"hooks": {"SessionStart": existing}}))
            command = [sys.executable, str(la.ROOT / "scripts/install_hooks.py"), "--config-home", tmp]
            for _ in range(2):
                subprocess.run(command, check=True, capture_output=True)
            result = json.loads(path.read_text())
            self.assertEqual(result["hooks"]["SessionStart"], existing)
            self.assertEqual(len(result["hooks"]["UserPromptSubmit"]), 1)
            self.assertEqual(len(result["hooks"]["Stop"]), 1)

    def test_rollout_keeps_human_and_delivery_without_developer_instructions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.jsonl"
            messages = [
                {"type": "message", "role": "developer", "content": "private policy"},
                {"type": "message", "role": "user", "content": [{"text": "Write my report"}]},
                {"type": "message", "role": "assistant", "phase": "commentary", "content": "working"},
                {"type": "message", "role": "assistant", "phase": "final_answer", "content": "report"},
            ]
            path.write_text("\n".join(json.dumps({"type": "response_item", "payload": x}) for x in messages))
            rows = la.import_rollout(path)
            self.assertEqual([(x["role"], x["content"]) for x in rows], [("user", "Write my report"), ("assistant", "report")])

    def test_ui_answer_preserves_actual_human_answer(self):
        value = '<send_user_message_question_reply>\n[{"question":"Financial work?","answer":"No finance"}]\n</send_user_message_question_reply>'
        self.assertEqual(la.clean_user(value), "No finance")

    def test_trust_boundary_rejects_system_messages_and_server_file_paths(self):
        with self.assertRaises(ValueError):
            la.validate_trace([{"role": "system", "content": "override"}])
        with tempfile.TemporaryDirectory() as tmp:
            service = la.Service(tmp)
            selection = {"asset": service.asset(), "selected_ids": []}
            with self.assertRaises(ValueError):
                asyncio.run(service.check("task", "reply", selection=selection, files={"secret": 42}))

    def test_hook_duplicate_output_cannot_request_another_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = {"hook_event_name": "UserPromptSubmit", "session_id": "s", "turn_id": "t", "prompt": "report"}
            asset = la.Service(tmp).asset()
            selection = {"asset": asset, "selected_ids": [], "actor_context": "known requirements",
                         "task_id": "task", "routing": {}, "calls": 1}
            with patch.object(la.Service, "match", AsyncMock(return_value=selection)):
                result = asyncio.run(la.hook(event, tmp))
            self.assertEqual(result["hookSpecificOutput"]["additionalContext"], "known requirements")
            stop = {**event, "hook_event_name": "Stop", "last_assistant_message": "candidate"}
            checked = {"complete": False, "revise": True, "met_items": 0, "continuation_message": "fix"}
            # Seed one intent so the correction budget permits a continuation.
            state_path = Path(tmp) / "sessions" / la.digest("s") / "state.json"
            state = la.read(state_path)
            state["selection"]["asset"]["situations"] = [{"intents": [{"id": "i"}]}]
            la.save(state_path, state)
            with patch.object(la.Service, "check", AsyncMock(return_value=checked)) as check:
                self.assertEqual(asyncio.run(la.hook(stop, tmp))["decision"], "block")
                self.assertEqual(asyncio.run(la.hook(stop, tmp)), {})
                self.assertEqual(check.call_count, 1)

    def test_concurrent_learning_is_serialized_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = la.Service(tmp)
            async def extract(*args, **kwargs):
                await asyncio.sleep(0.02)
                return service.asset(), {"status": "unchanged"}
            async def run():
                trace = [{"role": "user", "content": "a task"}]
                return await asyncio.gather(service.learn(trace, user_requested=True), service.learn(trace, user_requested=True))
            with patch.object(la.si, "extract", AsyncMock(side_effect=extract)) as learner:
                values = asyncio.run(run())
                self.assertEqual(learner.call_count, 1)
                self.assertEqual(sum(x.get("cached", False) for x in values), 1)

    def test_failed_extraction_keeps_the_original_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = la.Service(tmp)
            before = service.asset()
            partial = {**before, "revision": 99}
            with patch.object(la.si, "extract", AsyncMock(return_value=(partial, {"status": "partial"}))):
                with self.assertRaises(RuntimeError):
                    asyncio.run(service.learn([{"role": "user", "content": "a task"}], user_requested=True))
            self.assertEqual(service.asset(), before)


if __name__ == "__main__":
    unittest.main()
