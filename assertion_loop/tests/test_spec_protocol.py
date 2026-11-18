"""Offline wire-protocol checks for evidence reviews of GPT spec questions."""
from __future__ import annotations
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from looplib.common import digest,read
from looplib.llm import (Client,ModelProtocolError,ResponseFormatError,
                         assertion_blocks,call_json,json_content,response_schema)


class SpecReviewBlocksTests(unittest.TestCase):
    def test_review_keeps_expression_out_of_wire_item(self):
        result=assertion_blocks("BEGIN_REVIEW spec_1\nThe spec says ack follows req.\nThe unchanged assertion expresses that behavior.\nEND_REVIEW",
                                ["spec_1"],["spec_1"])
        self.assertEqual(result,{"assertions":[{"repair_id":"spec_1","review":"The spec says ack follows req.\nThe unchanged assertion expresses that behavior."}]})
        self.assertNotIn("expression",result["assertions"][0])

    def test_mixed_repair_and_spec_review(self):
        result=assertion_blocks("```text\r\nBEGIN_ASSERTION syntax_1\r\nreq |-> ack\r\nEND_ASSERTION\r\nBEGIN_REVIEW spec_1\r\nThe provided clause supports keeping this check.\r\nEND_REVIEW\r\n```",
                                ["syntax_1","spec_1"],["spec_1"])
        self.assertEqual(result["assertions"][0],{"expression":"req |-> ack","basis":"","repair_id":"syntax_1"})
        self.assertEqual(result["assertions"][1]["repair_id"],"spec_1")

    def test_technical_or_unknown_repair_cannot_use_review(self):
        for identifier,allowed in (("syntax_1",["spec_1"]),("unknown",["spec_1"]),("spec_1",[])):
            with self.subTest(identifier=identifier,allowed=allowed),self.assertRaises(ValueError):
                assertion_blocks(f"BEGIN_REVIEW {identifier}\nKeep it.\nEND_REVIEW",["syntax_1","spec_1"],allowed)

    def test_duplicate_incomplete_nested_or_outside_text_is_rejected(self):
        invalid=[
            "BEGIN_ASSERTION spec_1\nreq |-> ack\nEND_ASSERTION\nBEGIN_REVIEW spec_1\nKeep.\nEND_REVIEW",
            "BEGIN_REVIEW spec_1\nEvidence without an end",
            "BEGIN_REVIEW spec_1\n\nEND_REVIEW",
            "BEGIN_REVIEW spec_1\nBEGIN_ASSERTION syntax_1\nreq |-> ack\nEND_ASSERTION\nEND_REVIEW",
            "BEGIN_ASSERTION syntax_1\nBEGIN_REVIEW spec_1\nKeep.\nEND_REVIEW\nEND_ASSERTION",
            "Prose outside\nBEGIN_REVIEW spec_1\nKeep.\nEND_REVIEW",
            "BEGIN_REVIEW spec_1\nKeep.\nEND_REVIEW\ntrailing prose",
            "BEGIN_REVIEW spec_1\nKeep.\nEND_ASSERTION",
        ]
        for text in invalid:
            with self.subTest(text=text),self.assertRaises(ValueError):
                assertion_blocks(text,["syntax_1","spec_1"],["spec_1"])

    def test_legacy_assertion_json_and_permitted_review_json(self):
        historical={"assertions":[{"expression":"req |-> ack","basis":"existing response"}]}
        self.assertEqual(assertion_blocks(json.dumps(historical)),historical)
        review={"assertions":[{"repair_id":"spec_1","review":"The cited spec clause supports this check."}]}
        self.assertEqual(assertion_blocks(json.dumps(review),["spec_1"],["spec_1"]),review)
        for payload in (
            {"assertions":[{"repair_id":"syntax_1","review":"Keep."}]},
            {"assertions":[{"repair_id":"spec_1","review":""}]},
            {"assertions":[{"repair_id":"spec_1","review":"Keep.","expression":"req |-> ack"}]},
            {"assertions":[{"repair_id":"spec_1","review":"Keep."},{"repair_id":"spec_1","expression":"req |-> ack"}]},
        ):
            with self.subTest(payload=payload),self.assertRaises(ValueError):
                assertion_blocks(json.dumps(payload),["syntax_1","spec_1"],["spec_1"])

    def test_spec_schema_requests_suggestion_and_legacy_json_stays_readable(self):
        schema=response_schema("spec_check",{})
        self.assertEqual(schema["properties"]["suggested_change"],{"type":"string"})
        self.assertIn("suggested_change",schema["required"])
        self.assertEqual(json_content('{"verdict":"PASS"}'),{"verdict":"PASS"})
        generation=response_schema("generation",{"repair_ids":["syntax_1","spec_1"],"review_ids":["spec_1"]})
        reviews=generation["properties"]["assertions"]["items"]["anyOf"][1]
        self.assertEqual(reviews["properties"]["repair_id"]["enum"],["spec_1"])

    def test_format_only_recovery_preserves_review_and_suggestion(self):
        cases=[
            ("generation",'{"assertions":[{"repair_id":"spec_1","review":"Keep the check.", bad: 0}]}',
             {"assertions":[{"repair_id":"spec_1","review":"Keep the check."}]},"review"),
            ("spec_check",'{"verdict":"FAIL","suggested_change":"Use the next cycle.", bad: 0}',
             {"verdict":"FAIL","suggested_change":"Use the next cycle."},"suggested_change"),
        ]
        with tempfile.TemporaryDirectory() as folder,redirect_stdout(io.StringIO()):
            for purpose,original,repaired,field in cases:
                calls=[]
                def call(*args):
                    calls.append(args)
                    if len(calls)==1: raise ResponseFormatError("Malformed JSON",original)
                    return repaired
                result=call_json(SimpleNamespace(call=call),"deepseek",purpose,[],{},Path(folder)/purpose)
                self.assertEqual(result,repaired)
                self.assertIn(field,calls[1][2][0]["content"])
                changed=json.loads(json.dumps(repaired))
                target=changed["assertions"][0] if purpose=="generation" else changed
                target[field]="Unapproved replacement"
                calls.clear()
                def corrupt_call(*args):
                    calls.append(args)
                    if len(calls)==1: raise ResponseFormatError("Malformed JSON",original)
                    return changed
                with self.assertRaises(ModelProtocolError):
                    call_json(SimpleNamespace(call=corrupt_call),"deepseek",purpose,[],{},Path(folder)/(purpose+"_changed"))


class SpecReviewClientTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config={"base_url":"https://example.invalid/api/v1","models":{
            "deepseek":{"id":"offline/deepseek"},"gpt":{"id":"offline/gpt"}}}
        self.env=patch.dict(os.environ,{"OPENROUTER_API_KEY":"OFFLINE_TEST_KEY"})
        self.env.start();self.addCleanup(self.env.stop)
        self.client=Client(self.config,Path(self.temp.name)/"api")

    @staticmethod
    def reply(content):
        return {"id":"offline-review","choices":[{"message":{"content":content},"finish_reason":"stop"}],
                "usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15,"cost":0}}

    def test_api_passes_review_ids_to_parser_and_preserves_context(self):
        context={"generator_model":"deepseek","round":1,"step_a_attempt":2,
                 "repair_ids":["spec_1"],"review_ids":["spec_1"],"output_protocol":"assertion_blocks","action":"repair_existing"}
        text="BEGIN_REVIEW spec_1\nThe cited clause supports the unchanged assertion.\nEND_REVIEW"
        terminal=io.StringIO()
        with patch.object(self.client,"request",return_value=self.reply(text)) as request,redirect_stdout(terminal):
            result=self.client.call("deepseek","generation",[{"role":"user","content":"Review this opinion."}],context)
        self.assertEqual(request.call_count,1)
        self.assertEqual(result["assertions"][0]["review"],"The cited clause supports the unchanged assertion.")
        record=self.client.records()[0]
        saved=read(self.client.output/"requests"/(record["request_sha256"]+".json"))
        self.assertEqual(saved["context"]["review_ids"],["spec_1"])
        self.assertEqual(record["request_context"],context)
        self.assertEqual(record["purpose"],"generation")
        self.assertIn("repair_existing model=deepseek",terminal.getvalue())
        self.assertIn("purpose=repair_existing",terminal.getvalue())
        self.assertEqual(record["request_sha256"],digest({"body":saved["body"],"context":context,"purpose":"generation"}))
        self.assertNotIn("max_tokens",saved["body"])

    def test_legacy_context_reconstruction_includes_review_ids(self):
        messages=[{"role":"user","content":"Check the assertion and cited evidence."}]
        first_context={"round":1,"review_ids":["spec_1"],"action":"review_spec"}
        with patch.object(self.client,"request",return_value=self.reply('{"verdict":"PASS"}')) as request,redirect_stdout(io.StringIO()):
            self.client.call("gpt","spec_check",messages,first_context)
            record=self.client.records()[0]
            record.pop("request_context")
            self.client.ledger.write_text(json.dumps(record)+"\n")
            result=self.client.call("gpt","spec_check",messages,{**first_context,"round":2,"reuse_cached_response":True})
        self.assertEqual(result,{"verdict":"PASS"})
        self.assertEqual(request.call_count,1)
        reuse=json.loads((self.client.output/"cache_reuses.jsonl").read_text())
        self.assertEqual(reuse["source_context"]["review_ids"],["spec_1"])
        self.assertEqual(reuse["source_context"]["action"],"review_spec")


if __name__=="__main__": unittest.main()
