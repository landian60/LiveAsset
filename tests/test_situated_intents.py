import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from liveasset import situated_intents as si


DIALOGUE = [
    {"role": "user", "content": "Please organize my labmate's feedback on the LiteAlign draft."},
    {"role": "assistant", "content": "Here is a summary of the comments."},
    {"role": "user", "content": "Relabel every comment using exactly [Additional experiments needed], "
                                "[Clarification needed], or [Typos / errors]."},
    {"role": "assistant", "content": "Relabelled."},
    {"role": "user", "content": "With one week left, defer the heavy experiments to future work."},
]
QUERY = "Please organize my labmate's feedback on the LiteAlign draft."

INTENTS = [
    {"id": "label-comments",
     "intent_explanation": "Organizing reviewer feedback on a draft, when the result groups comments "
                                   "without the user's labels; the user wants follow-up work assignable.",
     "requirements": ["Label each comment with exactly one of the user's categories."],
     "facts": [{"fact": "Comment labels: Additional experiments needed, Clarification needed, Typos / errors",
                "quote": "Relabel every comment using exactly"}]},
    {"id": "defer-heavy",
     "intent_explanation": "Once comments ask for experiments, when the result promises all of them; "
                                   "the user wants infeasible work handled gracefully.",
     "requirements": ["Tactfully defer heavy experiments to future work."],
     "facts": [{"fact": "Time left before the deadline: one week", "quote": "With one week left"}]},
]


def extraction(intents=INTENTS, changes=None, situation_id="peer-feedback", query=QUERY):
    return {"situation_id": situation_id, "query": query, "intents": intents,
            "changes": changes or [{"op": "add", "intent_id": item["id"], "merged_from": [], "reason": "new"}
                                   for item in intents]}


def situations(asset, text="Organizing reviewer feedback on a paper draft into an actionable response."):
    return {"situations": [{"situation_id": s["id"], "situation": text, "merged": []} for s in asset["situations"]]}


class Model:
    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    async def chat(self, messages):
        self.prompts.append(messages[0]["content"])
        reply = self.replies.pop(0)
        if callable(reply):
            reply = reply(messages[0]["content"])
        return SimpleNamespace(content=reply if isinstance(reply, str) else json.dumps(reply))


def payload(prompt):
    return json.loads(prompt.split("INPUT:\n", 1)[1])


class SituatedIntentTest(unittest.TestCase):
    def learned(self):
        asset, _ = si.compile_extraction(extraction(), asset=si.new_situated_asset("u", "u"),
                                         dialogue=DIALOGUE, episode="e")
        return si.compile_situations(situations(asset), asset)

    def test_step_adds_a_group_keyed_by_query_and_facts_keep_user_quotes(self):
        asset = self.learned()
        si.validate_asset(asset)
        group = asset["situations"][0]
        self.assertEqual(group["queries"], [QUERY])
        self.assertTrue(group["situation"].startswith("Organizing reviewer feedback"))
        self.assertEqual([i["id"] for i in group["intents"]], ["label-comments", "defer-heavy"])
        self.assertEqual(group["intents"][1]["facts"][0]["episode"], "e")
        self.assertEqual(set(group["intents"][0]), {"id", "intent_explanation", "requirements", "facts"})

    def test_legacy_v2_load_preserves_original_and_scoped_facts(self):
        asset = self.learned()
        del asset["situations"][0]["practices"]
        fact = asset["situations"][0]["intents"][0]["facts"][0]
        fact["scope"] = "task"
        original = json.dumps(asset, sort_keys=True)
        loaded = si.validate_asset(asset)
        self.assertEqual(json.dumps(asset, sort_keys=True), original)
        self.assertEqual(loaded["situations"][0]["practices"], [])
        self.assertEqual(loaded["situations"][0]["intents"][0]["facts"][0], fact)
        self.assertTrue(si.injection(loaded, ["label-comments"]))
        for extra in [{"scope": "all"}, {"unexpected": "value"}]:
            broken = json.loads(original)
            broken["situations"][0]["intents"][0]["facts"][0].update(extra)
            with self.assertRaises(ValueError):
                si.validate_asset(broken)

    def test_facts_are_checker_items_and_one_known_list(self):
        asset = self.learned()
        context = si.injection(asset, ["defer-heavy"])
        self.assertIn("- Known: Time left before the deadline: one week", context)
        progress = si.IntentProgress(asset, ["defer-heavy"])
        check = progress._prompt(["defer-heavy"], {})
        self.assertEqual(payload(check)["nodes"][0]["facts"], [{"id": "f1", "text": "Time left before the deadline: one week"}])
        self.assertIn("applies only when this task is that same occasion", check)
        with self.assertRaisesRegex(ValueError, "fact must be"):
            si.compile_extraction(extraction([{**INTENTS[1], "facts": [{"fact": "x", "quote": "With one week left",
                                                                       "scope": "user"}]}]),
                                  asset=si.new_situated_asset("u", "u"), dialogue=DIALOGUE, episode="e")

    def test_facts_may_quote_tool_results_but_not_unseen_text(self):
        dialogue = DIALOGUE + [{"role": "tool", "name": "kb_get_article", "arguments": {},
                                "result": "| Priority label | high | critical |"}]
        facts = [{"fact": "Current SOP priority label: critical", "quote": "| high | critical |"},
                 {"fact": "Invented value", "quote": "not in the dialogue"}]
        asset, _ = si.compile_extraction(extraction([{**INTENTS[0], "facts": facts}]),
                                         asset=si.new_situated_asset("u", "u"), dialogue=dialogue, episode="e")
        self.assertEqual([f["fact"] for f in asset["situations"][0]["intents"][0]["facts"]],
                         ["Current SOP priority label: critical"])
        # a tool result is not a user request: a new group still needs a user-quoted query
        with self.assertRaisesRegex(ValueError, "query"):
            si.compile_extraction(extraction(query="| Priority label | high"), asset=si.new_situated_asset("u", "u"),
                                  dialogue=dialogue, episode="e")

    def test_requirements_do_not_restate_values_read_from_material(self):
        dialogue = DIALOGUE + [{"role": "tool", "name": "read_file", "arguments": {},
                                "result": "helpers: `merge_rows` in tools/merge_v2.py"}]
        fact = {"fact": "The helper was merge_rows", "quote": "`merge_rows` in tools/merge_v2.py"}
        asset, _ = si.compile_extraction(extraction([{**INTENTS[0], "facts": [fact]}]),
                                         asset=si.new_situated_asset("u", "u"), dialogue=dialogue, episode="e")
        self.assertEqual(asset["situations"][0]["intents"][0]["facts"][0]["fact"], "The helper was merge_rows")
        restating = {**INTENTS[0], "requirements": ["Merge through `merge_rows` in tools/merge_v2.py."],
                     "facts": [fact]}
        with self.assertRaisesRegex(ValueError, "restates values read from this task's material"):
            si.compile_extraction(extraction([restating]), asset=si.new_situated_asset("u", "u"),
                                  dialogue=dialogue, episode="e")
        by_role = {**restating, "requirements": ["Merge through the helper the facts name."]}
        si.compile_extraction(extraction([by_role]), asset=si.new_situated_asset("u", "u"), dialogue=dialogue,
                              episode="e")

    def test_rejects_silent_drop_missing_fields_and_unquoted_new_group(self):
        asset = self.learned()
        with self.assertRaisesRegex(ValueError, "dropped"):
            si.compile_extraction(extraction([INTENTS[0]], [{"op": "keep", "intent_id": "label-comments",
                                                             "merged_from": [], "reason": "k"}]),
                                  asset=asset, dialogue=DIALOGUE, episode="e2")
        bad = {**INTENTS[0], "goal": "x"}
        with self.assertRaisesRegex(ValueError, "fields"):
            si.compile_extraction(extraction([bad]), asset=si.new_situated_asset("u", "u"),
                                  dialogue=DIALOGUE, episode="e")
        with self.assertRaisesRegex(ValueError, "query"):
            si.compile_extraction(extraction(query="Write me a poem."), asset=si.new_situated_asset("u", "u"),
                                  dialogue=DIALOGUE, episode="e")

    def test_step_updates_only_its_own_group(self):
        asset = self.learned()
        other = [{"id": "poem-rhyme", "intent_explanation": "Writing poems; the user wants rhyme.",
                  "requirements": ["Rhyme the lines."], "facts": []}]
        poem = [{"role": "user", "content": "Write a short poem about rain."}, {"role": "assistant", "content": "..."},
                {"role": "user", "content": "Make it rhyme."}]
        asset, _ = si.compile_extraction(extraction(other, situation_id="poems", query="Write a short poem about rain."),
                                         asset=asset, dialogue=poem, episode="e2")
        self.assertEqual([s["id"] for s in asset["situations"]], ["peer-feedback", "poems"])
        self.assertEqual(len(asset["situations"][0]["intents"]), 2)
        revised = [{**INTENTS[0], "requirements": ["Label each comment once.", "Keep the original wording."]},
                   INTENTS[1]]
        changes = [{"op": "revise", "intent_id": "label-comments", "merged_from": [], "reason": "r"},
                   {"op": "keep", "intent_id": "defer-heavy", "merged_from": [], "reason": "k"}]
        asset, _ = si.compile_extraction(extraction(revised, changes), asset=asset, dialogue=DIALOGUE, episode="e3")
        self.assertEqual(asset["situations"][1]["intents"][0]["id"], "poem-rhyme")
        self.assertEqual(len(asset["situations"][0]["intents"][0]["requirements"]), 2)
        # a kept fact whose quote this step's dialogue lacks keeps its original episode
        poem_step = [{"role": "user", "content": "Also add a reviewer column."}]
        asset2, _ = si.compile_extraction(extraction(revised, changes), asset=asset, dialogue=poem_step, episode="e4")
        self.assertEqual(asset2["situations"][0]["intents"][1]["facts"][0]["episode"], "e3")

    def test_write_situations_merges_same_kind_and_covers_every_group(self):
        asset = self.learned()
        poem = [{"role": "user", "content": "Summarize reviewer comments on my second paper."}]
        asset, _ = si.compile_extraction(
            extraction([{**INTENTS[0], "id": "summarize-comments", "facts": []}], situation_id="second",
                       query="Summarize reviewer comments on my second paper."),
            asset=asset, dialogue=poem, episode="e2")
        merged = si.compile_situations({"situations": [{"situation_id": "peer-feedback", "situation": "Reviews.",
                                                        "merged": ["second"]}]}, asset)
        self.assertEqual(len(merged["situations"]), 1)
        self.assertEqual(len(merged["situations"][0]["queries"]), 2)
        with self.assertRaisesRegex(ValueError, "leave groups out"):
            si.compile_situations({"situations": [{"situation_id": "peer-feedback", "situation": "R.",
                                                   "merged": []}]}, asset)
        prompt = si.situation_prompt(asset)
        self.assertIn("intent_explanations", prompt)
        self.assertIn("Summarize reviewer comments", prompt)

    def test_each_step_is_told_the_situation_the_previous_step_returned(self):
        async def run(root):
            first = extraction([INTENTS[0]])
            # the second step keeps the same situation without adding anything
            second = {"situation_id": "peer-feedback", "query": "", "intents": [INTENTS[0]],
                      "changes": [{"op": "keep", "intent_id": "label-comments", "merged_from": [], "reason": "k"}]}
            model = Model([first, second, {"practices": []},
                           lambda p: situations({"situations": [{"id": g["situation_id"]} for g in payload(p)["groups"]]})])
            await si.extract(model, si.new_situated_asset("u", "u"), [dict(r) for r in DIALOGUE], episode="e", log_dir=root)
            self.assertIsNone(payload(model.prompts[0])["current_situation"])
            self.assertEqual(payload(model.prompts[1])["current_situation"], "peer-feedback")
        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(run(Path(tmp)))

    def test_failed_extraction_keeps_the_previous_asset(self):
        async def run(root):
            seed = si.new_situated_asset("u", "u")
            model = Model(["not json", "{}", {"situation_id": "x"}])
            asset, receipt = await si.extract(model, seed, DIALOGUE[:3], episode="e", log_dir=root)
            self.assertEqual(asset, seed)
            self.assertEqual(receipt["status"], "failed")
        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(run(Path(tmp)))

    def test_incremental_parts_span_the_result_before_and_after_each_correction(self):
        parts = si.dialogue_parts(DIALOGUE)
        self.assertEqual([[row["role"] for row in part] for part, _ in parts],
                         [["user", "assistant", "user", "assistant"], ["assistant", "user"]])
        self.assertEqual(parts[1][1], [DIALOGUE[0]["content"], DIALOGUE[2]["content"]])

    def test_incremental_extraction_then_writes_situations_once(self):
        async def run(root):
            keep = [{"op": "keep", "intent_id": "label-comments", "merged_from": [], "reason": "k"},
                    {"op": "add", "intent_id": "defer-heavy", "merged_from": [], "reason": "new"}]
            model = Model([extraction([INTENTS[0]]), extraction(INTENTS, keep),
                           {"practices": ["Group the comments before labelling them."]},
                           lambda p: situations({"situations": [{"id": g["situation_id"]} for g in payload(p)["groups"]]})])
            trace = [dict(row) for row in DIALOGUE]
            asset, receipt = await si.extract(model, si.new_situated_asset("u", "u"), trace, episode="e", log_dir=root)
            self.assertEqual(receipt["status"], "updated")
            self.assertEqual((receipt["situations"], receipt["practices"]), ("written", "written"))
            self.assertEqual(len(model.prompts), 4)
            self.assertEqual(asset["situations"][0]["practices"], ["Group the comments before labelling them."])
            self.assertIn("never had to ask for", model.prompts[2])
            self.assertFalse(payload(model.prompts[2])["work_used_tools"])
            self.assertIn("earlier_user_messages", payload(model.prompts[1]))
            self.assertTrue(asset["situations"][0]["situation"].startswith("Organizing reviewer feedback"))
            self.assertEqual(len(asset["situations"][0]["intents"]), 2)
        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(run(Path(tmp)))

    def test_practices_are_injected_as_guidance_and_never_checked(self):
        asset = self.learned()
        asset["situations"][0]["practices"] = ["Group the comments before labelling them."]
        context = si.injection(asset, ["defer-heavy"])
        self.assertIn("Ways of working that served this user before", context)
        self.assertIn("- Group the comments before labelling them.", context)
        progress = si.IntentProgress(asset, ["defer-heavy"])
        self.assertNotIn("Group the comments", progress._prompt(["defer-heavy"], {}))
        self.assertIsNone(si.injection(asset, []))
        with self.assertRaisesRegex(ValueError, "at most"):
            si.compile_practices({"practices": ["p"] * (si.MAX_PRACTICES + 1)}, asset, "peer-feedback")

    def test_messages_mode_learns_the_same_steps_from_user_messages_alone(self):
        async def run(root):
            keep = [{"op": "keep", "intent_id": "label-comments", "merged_from": [], "reason": "k"},
                    {"op": "add", "intent_id": "defer-heavy", "merged_from": [], "reason": "new"}]
            model = Model([extraction([INTENTS[0]]), extraction(INTENTS, keep),
                           lambda p: situations({"situations": [{"id": g["situation_id"]} for g in payload(p)["groups"]]})])
            trace = [dict(row) for row in DIALOGUE]
            asset, receipt = await si.extract(model, si.new_situated_asset("u", "u"), trace, episode="e",
                                              log_dir=root, mode="messages")
            self.assertEqual(receipt["mode"], "messages")
            self.assertEqual(len(model.prompts), 3)
            steps = [payload(prompt) for prompt in model.prompts[:2]]
            self.assertEqual([[row["role"] for row in step["dialogue"]] for step in steps],
                             [["user", "user"], ["user"]])
            self.assertEqual(steps[1]["earlier_user_messages"], [DIALOGUE[0]["content"], DIALOGUE[2]["content"]])
            self.assertNotIn("Relabelled.", model.prompts[1])
            self.assertIn("are not shown", model.prompts[0])
            self.assertNotIn("find the cause in what the agent", model.prompts[0])
            self.assertEqual(len(asset["situations"][0]["intents"]), 2)
        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(run(Path(tmp)))
        full = si.extraction_prompt(self.learned(), DIALOGUE, [])
        self.assertIn("find the cause in what the agent", full)
        self.assertIn("Compare the two results", full)

    def test_match_selects_situations_and_injects_all_their_intents(self):
        async def run(root):
            asset = self.learned()
            model = Model([{"selected": [{"situation_id": "peer-feedback", "reason": "Review feedback."}]}])
            result = await si.match(model, asset, "Sort the reviews of my other paper.", task_context=None, log_dir=root)
            self.assertEqual(result["selected_ids"], ["label-comments", "defer-heavy"])
            sent = payload(model.prompts[0])
            # Matching sees scope and intent purposes, without loading requirements or historical facts.
            self.assertEqual(sent["situations"], [{"situation_id": "peer-feedback",
                                                   "situation": asset["situations"][0]["situation"],
                                                   "intents": [{"id": i["id"], "intent_explanation": i["intent_explanation"]}
                                                               for i in asset["situations"][0]["intents"]]}])
            context = result["actor_context"]
            self.assertIn("Tactfully defer heavy experiments", context)
            self.assertIn("Known: Time left before the deadline: one week", context)
            self.assertIn("graceful", context)  # the explanation is injected by default
            none = await si.match(Model([{"selected": []}, {"selected": []}]), asset, "Book a flight.", task_context=None,
                                  log_dir=root)
            self.assertEqual(none["selected_ids"], [])
            self.assertIsNone(none["actor_context"])
            bad = await si.match(Model(["x", "y"]), asset, "Sort reviews.", task_context=None, log_dir=root)
            self.assertEqual((bad["status"], bad["selected_ids"]), ("failed", []))
        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(run(Path(tmp)))

    def test_an_empty_match_is_asked_once_more(self):
        async def run(root):
            asset = self.learned()
            model = Model([{"selected": []}, {"selected": [{"situation_id": "peer-feedback", "reason": "r"}]}])
            result = await si.match(model, asset, "Sort the reviews.", task_context=None, log_dir=root)
            self.assertTrue(result["reasked_empty"])
            self.assertEqual(result["selected_ids"], ["label-comments", "defer-heavy"])
            none = await si.match(Model([{"selected": []}, {"selected": []}]), asset, "Book a flight.",
                                  task_context=None, log_dir=root)
            self.assertEqual((none["selected_ids"], none["status"]), ([], "matched"))
        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(run(Path(tmp)))

    def test_injection_full_adds_explanations(self):
        asset = self.learned()
        text = si.injection(asset, ["defer-heavy"], "full")
        self.assertIn("handled gracefully", text)
        self.assertNotIn("Label each comment", text)
        self.assertNotIn("handled gracefully", si.injection(asset, ["defer-heavy"], "goal"))

    def check(self, progress, verdicts, root, name="p.json"):
        model = Model([{"nodes": verdicts}])
        return asyncio.run(progress.check(model, output="out", visible_trace=[], artifacts=None,
                                          log_path=root / name)), model

    def test_every_check_rejudges_all_intents_and_a_broken_met_one_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            progress = si.IntentProgress(self.learned(), ["label-comments", "defer-heavy"])
            result, model = self.check(progress, [
                {"intent_id": "label-comments", "status": "met", "reason": "labelled"},
                {"intent_id": "defer-heavy", "status": "not_applicable", "reason": "no experiments asked"}], root)
            self.assertFalse(result["revise"])
            self.assertTrue(result["complete"])
            self.assertEqual(result["not_applicable"], ["defer-heavy"])
            sent = payload(model.prompts[0])["nodes"]
            self.assertIn("intent_explanation", sent[0])
            self.assertNotIn("goal", sent[0])
            # next output: every intent is judged again, the not_applicable one is now unmet
            result, model = self.check(progress, [
                {"intent_id": "label-comments", "status": "met", "reason": "labelled"},
                {"intent_id": "defer-heavy", "status": "unmet", "reason": "promised every experiment"}], root, "q.json")
            self.assertEqual([n["intent_id"] for n in payload(model.prompts[0])["nodes"]],
                             ["label-comments", "defer-heavy"])
            self.assertTrue(result["revise"])
            self.assertIn("Tactfully defer heavy experiments", result["continuation_message"])
            self.assertIn("Known: Time left before the deadline", result["continuation_message"])
            self.assertIn("Already met, keep as is: Label each comment", result["continuation_message"])
            # a revision breaks the earlier met intent: it is judged unmet again
            result, _ = self.check(progress, [
                {"intent_id": "label-comments", "status": "unmet", "reason": "labels lost"},
                {"intent_id": "defer-heavy", "status": "met", "reason": "deferred"}], root, "r.json")
            self.assertEqual(progress.state["label-comments"], "unsatisfied")
            self.assertTrue(result["revise"])

    def test_items_are_judged_one_by_one_and_only_unmet_ones_are_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            progress = si.IntentProgress(self.learned(), ["label-comments", "defer-heavy"])
            sent = payload(progress._prompt(["defer-heavy"], {}))["nodes"][0]
            self.assertEqual(sent["requirements"], [{"id": "r1", "text": "Tactfully defer heavy experiments to future work."}])
            self.assertEqual(sent["facts"][0]["id"], "f1")
            result, _ = self.check(progress, [
                {"intent_id": "label-comments", "items": [{"id": "r1", "status": "not_applicable", "reason": "n/a"},
                                                          {"id": "f1", "status": "not_applicable", "reason": "n/a"}]},
                {"intent_id": "defer-heavy", "items": [{"id": "r1", "status": "met", "reason": "deferred"},
                                                       {"id": "f1", "status": "unmet", "reason": "ignores the week"}]}],
                root)
            # one item not applicable does not hide the others; one unmet item makes its intent unmet
            self.assertEqual(progress.state, {"label-comments": "not_applicable", "defer-heavy": "unsatisfied"})
            message = result["continuation_message"]
            self.assertIn("Not yet met: Known: Time left before the deadline: one week (ignores the week)", message)
            self.assertNotIn("Not yet met: Tactfully", message)
            self.assertIn("Already met, keep as is: Tactfully defer heavy experiments", message)

    def test_recheck_sees_only_the_reply_that_replaced_the_earlier_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            progress = si.IntentProgress(self.learned(), ["label-comments"])
            model = Model([{"nodes": [{"intent_id": "label-comments", "status": "unmet", "reason": "table lost"}]}])
            trace = [{"role": "user", "content": "first request"},
                     {"role": "assistant", "content": "earlier reply with the table"},
                     {"role": "user", "content": "correction"},
                     {"role": "assistant", "content": "reply before the continuation, with the table"},
                     {"role": "system", "kind": "intent_continuation", "content": "LiveAsset: continue"},
                     {"role": "tool", "kind": "tool_event", "name": "add", "arguments": {}, "result_summary": "ok"},
                     {"role": "assistant", "content": "cart summary only"}]
            asyncio.run(progress.check(model, output="cart summary only", visible_trace=trace, artifacts=None,
                                       log_path=Path(tmp) / "p.json"))
            replies = [row["content"] for row in payload(model.prompts[0])["dialogue"] if row["role"] == "assistant"]
            self.assertEqual(replies, ["earlier reply with the table", "cart summary only"])
            self.assertEqual(payload(model.prompts[0])["dialogue"][-2]["role"], "tool")

    def test_omitted_nodes_are_reasked_then_unknown_never_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            progress = si.IntentProgress(self.learned(), ["label-comments", "defer-heavy"])
            model = Model([{"nodes": [{"intent_id": "label-comments", "status": "met", "reason": "ok"}]},
                           {"nodes": []}])
            result = asyncio.run(progress.check(model, output="o", visible_trace=[], artifacts=None,
                                                log_path=Path(tmp) / "p.json"))
            self.assertEqual(len(model.prompts), 2)
            self.assertEqual(progress.state["defer-heavy"], "unknown")
            self.assertFalse(result["revise"])
            self.assertEqual(progress.errors, 1)

    def test_unselected_situations_are_never_added(self):
        asset = self.learned()
        progress = si.IntentProgress(asset, ["label-comments"])
        self.assertEqual(progress.order, ["label-comments"])

    def test_missing_fact_is_reasked_and_never_defaults_to_met(self):
        with tempfile.TemporaryDirectory() as tmp:
            for second, expected in [("not_applicable", "not_applicable"), ("unknown", "unknown")]:
                progress = si.IntentProgress(self.learned(), ["label-comments"])
                model = Model([
                    {"nodes": [{"intent_id": "label-comments", "items": [{"id": "r1", "status": "met"}]}]},
                    {"nodes": [{"intent_id": "label-comments", "items": [{"id": "f1", "status": second}]}]}])
                result = asyncio.run(progress.check(model, output="o", visible_trace=[], artifacts=None,
                                                    log_path=Path(tmp)/f"{second}.json"))
                self.assertEqual(progress.item_state[("label-comments", "f1")], expected)
                self.assertEqual(result["complete"], second != "unknown")
                self.assertFalse(result["revise"])
                self.assertEqual(len(model.prompts), 2)
                self.assertNotEqual(model.prompts[0], model.prompts[1])

    def test_partial_retry_preserves_an_explicit_unmet_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            progress = si.IntentProgress(self.learned(), ["label-comments"])
            model = Model([
                {"nodes": [{"intent_id": "label-comments", "items": [{"id": "r1", "status": "unmet", "reason": "missing labels"}]}]},
                {"nodes": [{"intent_id": "label-comments", "items": [
                    {"id": "r1", "status": "met"}, {"id": "f1", "status": "not_applicable"}]}]}])
            result = asyncio.run(progress.check(model, output="o", visible_trace=[], artifacts=None,
                                                log_path=Path(tmp)/"check.json"))
            self.assertTrue(result["revise"])
            self.assertEqual(progress.item_state[("label-comments", "r1")], "unsatisfied")
            self.assertEqual(progress.item_state[("label-comments", "f1")], "not_applicable")
            self.assertIn("missing labels", result["continuation_message"])

    def test_stored_asset_validation(self):
        asset = self.learned()
        si.validate_asset(asset)
        old = {**asset, "schema_version": "liveasset-situated-v1"}
        with self.assertRaises(ValueError):
            si.validate_asset(old)
        broken = json.loads(json.dumps(asset))
        broken["situations"][0]["intents"][0]["goal"] = "x"
        with self.assertRaises(ValueError):
            si.validate_asset(broken)

    def test_prompts_name_the_new_fields(self):
        prompt = si.extraction_prompt(self.learned(), DIALOGUE, [])
        for text in ("intent_explanation", "do not narrate them one by one", "exception stated for this intent",
                     "only after the user explicitly confirms", '"situation_id"',
                     "starts from its opening request alone", "the value itself is not known",
                     "the user rejected is a fact", 'in this task the spreadsheet listed four regions',
                     "a later task has no wrong result to repair"):
            self.assertIn(text, prompt)
        self.assertNotIn("procedure", prompt)
        check = si.IntentProgress(self.learned(), ["defer-heavy"])._prompt(["defer-heavy"], {})
        self.assertIn("not_applicable", check)
        self.assertIn("Do not use not_applicable for an item the result involves but fails", check)
        self.assertIn("judge each of its requirements and facts on its own, by id", check)
        self.assertIn("ask for nothing the requirements and facts do not state", check)
        self.assertIn("also those an earlier result met", check)
        self.assertIn("never holds both", prompt)
        self.assertIn("identifiers or tokens a run", prompt)
        self.assertIn("current_situation", prompt)
        self.assertIn("When the user asks for <kind of task request>", si.situation_prompt(self.learned()))
        self.assertIn("Keep groups apart only when", si.situation_prompt(self.learned()))


if __name__ == "__main__":
    unittest.main()
