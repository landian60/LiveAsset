import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_binding_and_learning_gate_over_standard_mcp(self):
        with tempfile.TemporaryDirectory() as tmp:
            params = StdioServerParameters(command=sys.executable,
                args=[str(Path(__file__).parent / "scripts/mcp_server.py")],
                env={**os.environ, "LIVEASSET_STORE": tmp})
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    tools = (await client.list_tools()).tools
                    self.assertEqual(len(tools), 4)
                    match = next(t for t in tools if t.name == "match_intents")
                    self.assertIn("task_id", match.inputSchema["properties"])

                    async def call(name, args):
                        result = await client.call_tool(name, args)
                        self.assertFalse(result.isError, result)
                        return json.loads(next(c.text for c in result.content if c.type == "text"))

                    first = await call("match_intents", {"request": "整理材料", "session_id": "a", "scope": "p", "event_id": "1"})
                    second = await call("match_intents", {"request": "继续", "session_id": "b", "scope": "p", "task_id": first["task_id"]})
                    self.assertEqual(second["task_id"], first["task_id"])
                    self.assertEqual(second["routing"]["action"], "reused")
                    self.assertEqual(second["calls"], 0)
                    checked = await call("check_delivery", {"request": "继续", "output": "结果", "task_id": first["task_id"], "scope": "p"})
                    self.assertEqual(checked["selected_ids"], [])
                    denied = await client.call_tool("learn_intents", {"trace": [{"role": "user", "content": "quoted correction"}]})
                    self.assertTrue(denied.isError)
                    self.assertFalse((Path(tmp) / "asset.json").exists())


if __name__ == "__main__":
    unittest.main()
