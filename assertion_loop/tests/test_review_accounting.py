"""Reviews are billed model calls, but do not generate assertion expressions."""
from __future__ import annotations
from contextlib import redirect_stdout
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from looplib.engine import generation_attempt
from looplib.llm import assertion_blocks


class ReviewAccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dataset=Path(self.temp.name)
        (self.dataset/"checker.sv").write_text(
            "module toy_assertions(input logic clk, rst, req, ack);\n"
            "/* LOOP_ASSERTIONS */\nendmodule\n")
        self.unit={"name":"toy","top":"toy","checker":"toy_assertions","template":"checker.sv",
                   "clock_mode":"clocked","clock":"clk","reset_expression":"rst","bind_files":[]}
        self.spec_item={"repair_id":"spec_1","label":"a_spec","expression":"req |-> ack",
                        "checks":{"syntax":"PASS","fpv":"PASS","vacuity":"PASS"}}
        self.spec_problem={**self.spec_item,"failed_stage":"spec","verdict":"FAIL",
                           "question":"Verify the requirement's timing.","suggested_change":"Check same-cycle timing."}
        self.technical_calls=[]

    def attempt(self,reply,*,mixed=False):
        held=[self.spec_item]
        problems=[self.spec_problem]
        if mixed:
            syntax_item={"repair_id":"syntax_1","label":"a_syntax","expression":"syntax_bad |-> ack","checks":{}}
            held.append(syntax_item)
            problems.append({**syntax_item,"failed_stage":"syntax","diagnostic":"Unknown signal syntax_bad"})
        def call(model,purpose,messages,context):
            if purpose=="generation":
                return assertion_blocks(reply,context["repair_ids"],context["review_ids"])
            self.assertEqual((model,purpose),("gpt","spec_check"))
            return {"verdict":"PASS","question":"","reason":"Review resolved the timing question.",
                    "evidence_quote":"Ack equals req.","suggested_change":""}
        def qualify(dataset,unit,properties,directory,config,*,stage):
            self.technical_calls.extend((prop["repair_id"],stage) for prop in properties)
            return [{"label":prop["label"],"expression":prop["expression"],"stage":stage,"passed":True,"status":"PASS"}
                    for prop in properties]
        feedback={"repair_questions":problems,"held_candidates":held}
        with patch("looplib.engine.qualify",side_effect=qualify),redirect_stdout(io.StringIO()):
            return generation_attempt(self.dataset,self.unit,"deepseek",{},SimpleNamespace(call=call),
                "Ack equals req.","assign ack = req;",(self.dataset/"checker.sv").read_text(),[],feedback,
                {"round":1,"step_a_attempt":2},self.dataset/"attempt",set(),SimpleNamespace(check=lambda:None))

    def test_review_only_creates_no_assertion_expression(self):
        result=self.attempt("BEGIN_REVIEW spec_1\nThe spec says ack equals req; same-cycle implication is correct.\nEND_REVIEW")
        self.assertEqual(result["problems"],[])
        self.assertEqual(len(result["accepted"]),1)
        self.assertEqual(result["accepted"][0]["expression"],"req |-> ack")
        self.assertEqual(self.technical_calls,[])
        self.assertEqual(result["generated_count"],0,"An explanation preserves an assertion rather than generating one")

    def test_mixed_reply_counts_only_its_expression_item(self):
        result=self.attempt(
            "BEGIN_ASSERTION syntax_1\nreq |=> ack\nEND_ASSERTION\n"
            "BEGIN_REVIEW spec_1\nThe requirement supports keeping the same-cycle assertion.\nEND_REVIEW",mixed=True)
        self.assertEqual(result["problems"],[])
        self.assertEqual(len(result["accepted"]),2)
        self.assertEqual(self.technical_calls,[("syntax_1","syntax"),("syntax_1","fpv"),("syntax_1","vacuity")])
        self.assertEqual(result["generated_count"],1,"A mixed reply has one expression and one review")


if __name__=="__main__": unittest.main()
