"""All four generators and the GPT-only judge use the same OpenRouter endpoint."""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from .common import digest, read, save, table

class ModelProtocolError(RuntimeError):
    """Model output cannot be consumed safely; keep checkpoints and stop."""

class RepeatedRequestError(ModelProtocolError):
    """Same complete request already returned; stop without paying again."""

class ResponseFormatError(ValueError):
    def __init__(self,message,raw_text,response_path=""):
        super().__init__(message)
        self.raw_text=raw_text
        self.response_path=str(response_path)


class IncompleteResponseError(ResponseFormatError,ModelProtocolError):
    """Incomplete text is saved, never accepted; the outer Step A owns retries."""


def recovery_prompt_text(raw):
    """Preserve every distinct reply block; compact exact redundant copies.

    The complete bytes remain in invalid_response.txt / the API response. This
    is diagnostic context only; compacted text is NEVER accepted as a verdict
    or a qualified assertion, and no assertion-count ceiling is introduced.
    """
    value=str(raw).rstrip()
    try:
        parsed=json.loads(value)
        # Compact JSON whitespace without touching string values.
        return json.dumps(parsed,ensure_ascii=False,separators=(",",":"))
    except ValueError:pass
    pattern=re.compile(r"^[ \t]*BEGIN_(ASSERTION|REVIEW)[ \t]+([A-Za-z_][A-Za-z0-9_.-]*)[ \t]*\n(.*?)^[ \t]*END_\1[ \t]*$",re.M|re.S)
    counts={};pieces=[];end=0;duplicates=0
    for match in pattern.finditer(value):
        pieces.append(value[end:match.start()]);kind,identifier,body=match.groups();key=(kind,body.strip())
        counts[key]=counts.get(key,0)+1
        if counts[key]==1:pieces.append(match[0])
        else:duplicates+=1
        end=match.end()
    if not duplicates:return value
    pieces.append(value[end:])
    return "".join(pieces)+"\nDIAGNOSTIC COMPACTION: "+str(duplicates)+" redundant complete blocks were omitted here; each distinct body is shown once. Full original reply remains saved on disk. This is rejected prior output, not a new candidate set."


def clean_json_format(text):
    """Remove comments/trailing commas OUTSIDE strings. Never edit SVA values."""
    chars=list(text); quoted=False; escaped=False; i=0
    while i<len(chars):
        c=chars[i]
        if quoted:
            if escaped: escaped=False
            elif c=="\\": escaped=True
            elif c=='"': quoted=False
            i+=1; continue
        if c=='"': quoted=True; i+=1; continue
        if c=="/" and i+1<len(chars) and chars[i+1] in {"/","*"}:
            start=i; kind=chars[i+1]; i+=2
            if kind=="/":
                while i<len(chars) and chars[i] not in "\r\n": i+=1
            else:
                end=text.find("*/",i)
                if end<0: raise ValueError("Unterminated JSON block comment")
                i=end+2
            for j in range(start,i):
                if chars[j] not in "\r\n": chars[j]=" "
            continue
        i+=1
    quoted=False; escaped=False
    for i,c in enumerate(chars):
        if quoted:
            if escaped: escaped=False
            elif c=="\\": escaped=True
            elif c=='"': quoted=False
        elif c=='"': quoted=True
        elif c==",":
            j=i+1
            while j<len(chars) and chars[j].isspace(): j+=1
            if j<len(chars) and chars[j] in "}]": chars[i]=" "
    return "".join(chars)


def unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result: raise ValueError(f"Duplicate JSON key: {key}")
        result[key]=value
    return result


def json_content(text,notes=None):
    value=str(text).strip()
    fence=re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```",value,re.S|re.I)
    if fence: value=fence[1]
    try:
        data=json.loads(value,object_pairs_hook=unique_object)
    except json.JSONDecodeError:
        cleaned=clean_json_format(value)
        if cleaned==value: raise
        data=json.loads(cleaned,object_pairs_hook=unique_object)
        if notes is not None: notes.append("removed comments/trailing commas outside string values")
    if not isinstance(data,dict): raise ValueError("Expected a JSON object")
    return data


def assertion_blocks(text,repair_ids=(),review_ids=()):
    """Read expression/review blocks without JSON escaping or SV files.

    Well-formed historical JSON replies remain readable. Incomplete blocks,
    duplicate IDs and outside prose are rejected as a whole batch. Reviews are
    permitted only for spec questions explicitly identified by the caller.
    """
    value=str(text).strip().replace("\r\n","\n")
    fence=re.fullmatch(r"```(?:systemverilog|verilog|sv|text|json)?\s*\n(.*?)\n```",value,re.S|re.I)
    if fence: value=fence[1].strip()
    if value.startswith("NO_NEW_ASSERTIONS"):
        if repair_ids: raise ValueError("NO_NEW_ASSERTIONS cannot discard a pending repair")
        reason=value[len("NO_NEW_ASSERTIONS"):].strip()
        if not reason:raise ValueError("NO_NEW_ASSERTIONS needs a reason")
        return {"assertions":[],"no_new_assertions":True,"reason":reason}
    value=re.sub(r"(?:^|\n)END_BATCH\s*$","",value).rstrip()
    if value.startswith("{"):
        data=json_content(value)
        items=data.get("assertions",[])
        reviews=[item for item in items if isinstance(item,dict) and "review" in item] if isinstance(items,list) else []
        if reviews:
            ids=[item.get("repair_id") for item in items if isinstance(item,dict)]
            if len([identifier for identifier in ids if identifier is not None])!=len(set(identifier for identifier in ids if identifier is not None)):
                raise ValueError("Duplicate assertion/review repair ID")
            for item in reviews:
                if item.get("repair_id") not in review_ids:
                    raise ValueError("Review must copy a pending spec-review repair_id")
                if not isinstance(item["review"],str) or not item["review"].strip() or "expression" in item:
                    raise ValueError("A review needs nonempty rationale/evidence and no expression")
        return data
    pattern=re.compile(r"^[ \t]*BEGIN_(ASSERTION|REVIEW)[ \t]+([A-Za-z_][A-Za-z0-9_.-]*)[ \t]*\n(.*?)^[ \t]*END_\1[ \t]*$",re.M|re.S)
    rows=[]; ids=[]; end=0
    for match in pattern.finditer(value):
        if value[end:match.start()].strip(): raise ValueError("Unexpected text outside assertion/review blocks")
        kind,identifier,content=match.groups()
        if identifier in ids: raise ValueError(f"Duplicate assertion/review block ID: {identifier}")
        if not content.strip() or re.search(r"^[ \t]*(?:BEGIN|END)_(?:ASSERTION|REVIEW)\b",content,re.M):
            raise ValueError("Empty, nested or unfinished assertion/review block")
        if kind=="REVIEW":
            if identifier not in review_ids: raise ValueError(f"Review must copy a pending spec-review repair_id: {identifier}")
            rows.append({"repair_id":identifier,"review":content.strip()})
        else:
            if repair_ids and identifier not in repair_ids: raise ValueError(f"Copy a pending repair_id: {identifier}")
            rows.append({"expression":content.strip(),"basis":"",**({"repair_id":identifier} if repair_ids else {})})
        ids.append(identifier); end=match.end()
    if not rows or value[end:].strip():
        raise ValueError("Return complete BEGIN_ASSERTION <id> / expression / END_ASSERTION or permitted BEGIN_REVIEW <repair_id> / rationale and evidence / END_REVIEW blocks only")
    return {"assertions":rows}


def response_schema(purpose,context):
    purpose=context.get("format_repair_for",purpose)
    if purpose=="spec_check":
        props={"verdict":{"type":"string","enum":["PASS","FAIL","UNRESOLVED"]},
               **{key:{"type":"string"} for key in ("question","reason","evidence_quote","suggested_change")}}
        return {"type":"object","properties":props,"required":list(props),"additionalProperties":False}
    props={"expression":{"type":"string"},"basis":{"type":"string"}}
    if context.get("repair_ids"):
        props["repair_id"]={"type":"string","enum":context["repair_ids"]}
    item={"type":"object","properties":props,"required":list(props),"additionalProperties":False}
    if context.get("review_ids"):
        review_props={"repair_id":{"type":"string","enum":context["review_ids"]},"review":{"type":"string"}}
        item={"anyOf":[item,{"type":"object","properties":review_props,"required":list(review_props),"additionalProperties":False}]}
    return {"type":"object","properties":{"assertions":{"type":"array","items":item}},
            "required":["assertions"],"additionalProperties":False}


def string_fields(text,keys):
    """Read complete JSON string values even if object punctuation is malformed."""
    tokens=list(re.finditer(r'"(?:[^"\\]|\\.)*"',text,re.S))
    values={key:[] for key in keys}
    for left,right in zip(tokens,tokens[1:]):
        try: key=json.loads(left[0])
        except ValueError: continue
        if key not in values or not re.fullmatch(r"\s*:\s*",text[left.end():right.start()]): continue
        try: values[key].append(json.loads(right[0]))
        except ValueError: continue
    return values


def call_json(client,key,purpose,messages,context,directory,validator=None):
    """One visible, separately billed format-only recovery; never a network retry.

    Technical Step A repairs are unlimited. Invalid transport/output is a
    different condition: if format recovery fails, stop with pending items saved.
    """
    directory=Path(directory); directory.mkdir(parents=True,exist_ok=True)
    try:
        result=client.call(key,purpose,messages,context)
        if validator:
            try: validator(result)
            except ValueError as error:
                raise ResponseFormatError(str(error),json.dumps(result,ensure_ascii=False)) from error
        return result
    except ResponseFormatError as error:
        original=error
    (directory/"invalid_response.txt").write_text(original.raw_text,encoding="utf-8")
    save(directory/"format_error.json",{"error":str(original),"raw_api_response":original.response_path})
    if purpose=="generation" and context.get("output_protocol")=="assertion_blocks":
        print(f"[FORMAT] assertion/review blocks INVALID: {original}; saved at {directory/'invalid_response.txt'}; back to model in Step A",flush=True)
        # The Step A loop keeps every pending obligation and asks the generator
        # again with the original design context. No same-model JSON formatter.
        raise original
    if isinstance(original,IncompleteResponseError):
        # A truncated verdict must be regenerated by GPT from the design inputs,
        # not completed by a formatter or sent to the assertion generator.
        raise original
    print(f"[FORMAT] {purpose} INVALID: {original}; no assertion has been revalidated",flush=True)
    print(f"[FORMAT] raw reply: {directory/'invalid_response.txt'}",flush=True)
    print(f"[FORMAT] format-only recovery model={key}; assertion values must remain unchanged",flush=True)
    repair_messages=[{"role":"system","content":
        "Repair only the JSON format of the supplied response. Return one JSON object, no comments or trailing commas. "
        "Preserve every expression, repair_id, review, basis, verdict, question, reason, evidence_quote and suggested_change string exactly. "
        "Do not invent assertions or change a verdict."},
        {"role":"user","content":f"ERROR:\n{original}\nSCHEMA:\n{json.dumps(response_schema(purpose,context))}\nINVALID RESPONSE:\n{original.raw_text}"}]
    save(directory/"format_repair_prompt.json",repair_messages)
    try:
        repaired=client.call(key,"format_repair",repair_messages,{**context,"format_repair_for":purpose})
        save(directory/"format_repair.json",repaired)
        if validator: validator(repaired)
        fields=("expression","repair_id","review","basis") if purpose=="generation" else ("verdict","question","reason","evidence_quote","suggested_change")
        before=string_fields(original.raw_text,fields)
        after=string_fields(json.dumps(repaired,ensure_ascii=False),fields)
        for field in fields:
            if before[field] and before[field]!=after[field]:
                raise ValueError(f"Format recovery changed {field} values")
        if purpose=="generation" and not (before["expression"] or before["review"]):
            raise ValueError("No complete original expression or review strings to preserve safely")
        if purpose=="spec_check" and not before["verdict"]:
            raise ValueError("No complete original verdict string to preserve safely")
    except (ResponseFormatError,ValueError,KeyError,TypeError,IndexError) as error:
        if isinstance(error,ResponseFormatError):
            (directory/"invalid_format_repair.txt").write_text(error.raw_text,encoding="utf-8")
        save(directory/"protocol_error.json",{"status":"STOPPED_RESPONSE_PROTOCOL","error":str(error),"original_error":str(original)})
        print(f"[FORMAT] STOP: {error}; pending assertions preserved; no Step B",flush=True)
        raise ModelProtocolError(f"Model response format could not be safely recovered; inspect {directory}") from error
    print("[FORMAT] recovered JSON; preserved expressions/verdict; continuing normal Step A checks",flush=True)
    return repaired


def totals(records):
    successful = [r for r in records if r.get("response_received")]
    return {"api_calls":len(records), "response_count":len(successful),
            "format_repair_calls":sum(r.get("purpose")=="format_repair" for r in records),
            "prompt_tokens":sum(r.get("prompt_tokens") or 0 for r in successful),
            "completion_tokens":sum(r.get("completion_tokens") or 0 for r in successful),
            "total_tokens":sum(r.get("total_tokens") or 0 for r in successful),
            "missing_usage_calls":sum(not r.get("usage_complete",False) for r in records),
            "reported_cost_usd":sum(r.get("reported_cost_usd") or 0 for r in successful),
            "reported_cost_missing_calls":sum(r.get("reported_cost_usd") is None for r in records),
            "estimated_cost_usd":sum(r.get("estimated_cost_usd") or 0 for r in successful),
            "estimated_cost_missing_calls":sum(r.get("estimated_cost_usd") is None for r in records)}

class Client:
    def __init__(self, config, output):
        self.config, self.output = config, Path(output)
        self.output.mkdir(parents=True,exist_ok=True)
        self.ledger = self.output / "usage.jsonl"
        self.models = config["models"]
        self.key = os.environ.get(config.get("key_env","OPENROUTER_API_KEY"),"").strip()
        key_file = config.get("key_file")
        if not self.key and key_file and Path(key_file).is_file():
            self.key = Path(key_file).read_text().strip()
        if not self.key: raise ValueError("Set OPENROUTER_API_KEY (all four models use OpenRouter)")
        if any(c in self.key for c in ('\r','\n')): raise ValueError("API key must be a single line")
        self.catalog = {}

    def request(self,path,body=None):
        """Same subprocess+curl transport as run_spec_check_four_models.py.

        Auth headers enter curl through stdin, keeping the key out of process
        arguments. Both /models GET and chat POST use curl's normal TLS trust.
        """
        if not shutil.which("curl"):
            raise RuntimeError("curl was not found; the original API calling method requires curl")
        url = self.config["base_url"].rstrip("/") + path
        timeout=self.config.get("request_timeout_seconds",900)
        argv=["curl","--silent","--show-error","--connect-timeout","30",
              "--max-time",str(timeout),"--write-out","\n%{http_code}",
              "--config","-",url]
        if self.config.get("ca_bundle"):
            ca=Path(self.config["ca_bundle"]).expanduser().resolve()
            if not ca.is_file(): raise ValueError(f"CA bundle does not exist: {ca}")
            argv.extend(["--cacert",str(ca)])
        if body is not None:
            argv.extend(["-d",json.dumps(body,ensure_ascii=False)])
        def config_quote(value):
            return value.replace("\\","\\\\").replace('"','\\"')
        headers=["Authorization: Bearer "+self.key,"Content-Type: application/json",
                 "X-OpenRouter-Title: Assertion Generation Loop","X-OpenRouter-Metadata: enabled"]
        stdin_config="".join('header = "'+config_quote(h)+'"\n' for h in headers)
        try:
            completed=subprocess.run(argv,input=stdin_config,capture_output=True,text=True,
                                     timeout=float(timeout)+10,check=False)
        except subprocess.TimeoutExpired:
            raise RuntimeError("curl request timed out; not automatically retried (billing may be unknown)") from None
        if completed.returncode:
            raise RuntimeError(f"curl failed ({completed.returncode}): "+completed.stderr.strip().replace(self.key,"[REDACTED]"))
        try:
            raw,status_text=completed.stdout.rsplit("\n",1)
            status=int(status_text)
        except (ValueError,TypeError):
            raise RuntimeError("Cannot parse curl HTTP status") from None
        if not 200<=status<300:
            raise RuntimeError(f"OpenRouter HTTP {status}: "+raw[:2000].replace(self.key,"[REDACTED]"))
        try:
            result=json.loads(raw)
        except json.JSONDecodeError:
            raise RuntimeError("OpenRouter returned a non-JSON response") from None
        if not isinstance(result,dict): raise RuntimeError("OpenRouter response must be a JSON object")
        return result

    def preflight(self,keys):
        data = self.request("/models")
        self.catalog = {m["id"]:m for m in data["data"]}
        missing = [self.models[k]["id"] for k in keys if self.models[k]["id"] not in self.catalog]
        if missing:
            raise ValueError("Configured model IDs are not available; edit config.json, no automatic model substitution: "+", ".join(missing))
        previous=self.output/"pricing_snapshot.json"
        if previous.is_file():
            snapshot=read(previous)
            save(self.output/("pricing_snapshot.previous_"+digest(snapshot)[:12]+".json"),snapshot)
        save(self.output / "pricing_snapshot.json", {"fetched_at_unix":time.time(),
             "source":self.config["base_url"].rstrip("/")+"/models",
             "models":{k:self.catalog[self.models[k]["id"]] for k in keys}})

    def replay_response(self,key,purpose,body,context):
        """Revalidation only: prove the old request body matches byte-for-byte.

        Independent generation rounds/repeats never use this cross-context path.
        Legacy request hashes can be verified by reconstructing their context.
        """
        if not context.get("reuse_cached_response") or purpose!="spec_check": return None
        legacy_keys=("dataset","generator_model","repeat","unit","round","step_b_round","step_a_attempt",
                     "step_a_repair","assertion","repair_ids","review_ids","action","format_repair_for","reuse_cached_response")
        for record in reversed(self.records()):
            if record.get("purpose")!=purpose or record.get("api_model_key")!=key or record.get("model_id")!=self.models[key]["id"]: continue
            if any(record.get(k)!=context[k] for k in ("dataset","generator_model","repeat","unit") if k in context): continue
            previous=record.get("request_context") or {k:record[k] for k in legacy_keys if k in record}
            fingerprint=digest({"body":body,"context":previous,"purpose":purpose})
            if fingerprint!=record.get("request_sha256"): continue
            path=self.output/"responses"/(fingerprint+".json")
            if not path.is_file(): continue
            try:
                raw=read(path)
                choice=raw["choices"][0]
                if choice.get("finish_reason") in {"length","content_filter"}: continue
                parsed=json_content(choice["message"]["content"])
                if parsed.get("verdict") not in {"PASS","FAIL","UNRESOLVED"}: continue
            except (ValueError,KeyError,TypeError,IndexError): continue
            event={"context":context,"purpose":purpose,"model_id":self.models[key]["id"],
                   "source_request_sha256":fingerprint,"source_context":previous,
                   "time_unix":time.time(),"new_api_charge":False}
            with (self.output/"cache_reuses.jsonl").open("a",encoding="utf-8") as handle:
                handle.write(json.dumps(event,ensure_ascii=False)+"\n")
            print(f"[CACHE] reused exact prior {purpose} response; no new API call; {path}",flush=True)
            return raw
        return None

    def call(self,key,purpose,messages,context):
        model = self.models[key]
        display_action=context.get("action",purpose)
        body = {"model":model["id"],"messages":messages,
                "stream":False,
                "usage":{"include":True}}
        if self.config.get("max_output_tokens") is not None:
            body["max_tokens"]=self.config["max_output_tokens"]
        supported = self.catalog.get(model["id"],{}).get("supported_parameters",[])
        blocks=purpose=="generation" and context.get("output_protocol")=="assertion_blocks"
        if not blocks and "structured_outputs" in supported and "response_format" in supported:
            body["response_format"]={"type":"json_schema","json_schema":{"name":"assertion_loop_response","strict":True,
                "schema":response_schema(purpose,context)}}
        elif not blocks and "response_format" in supported:
            body["response_format"]={"type":"json_object"}
        if "temperature" in supported and model.get("supports_temperature",key!="gpt"):
            body["temperature"]=self.config.get("temperature",0)
        reasoning=model.get("reasoning_mode","auto")
        info=self.catalog.get(model["id"],{}).get("reasoning") or {}
        if reasoning=="off" and "reasoning" in supported:
            if info.get("mandatory"):
                raise ValueError(f"{model['id']} requires reasoning; off is unsupported")
            body["reasoning"]={"enabled":False}
        elif reasoning!="auto" and reasoning!="off":
            efforts=info.get("supported_efforts")
            if "reasoning" not in supported or efforts is not None and reasoning not in efforts:
                raise ValueError(f"Unsupported reasoning mode {reasoning} for {model['id']}")
            body["reasoning"]={"effort":reasoning,"exclude":True}
        body["provider"]={"require_parameters":True}
        fingerprint = digest({"body":body,"context":context,"purpose":purpose})
        # Persist the exact body sent to the API, without auth. This lets a run
        # show whether failure feedback was sent instead of guessing from logs.
        save(self.output/"requests"/(fingerprint+".json"),{"request_sha256":fingerprint,
             "purpose":purpose,"context":context,"body":body})
        cache = self.output / "responses" / (fingerprint+".json")
        if self.config.get("stop_on_repeated_request",False) and not cache.exists() and purpose in {"generation","spec_check","format_repair"} and not context.get("reuse_cached_response"):
            scope=("dataset","generator_model","unit","repeat","round","feedback_condition","assertion")
            prior=next((r for r in reversed(self.records()) if r.get("purpose")==purpose and r.get("api_model_key")==key and
                        r.get("request_body_sha256")==digest(body) and all(r.get(k)==context.get(k) for k in scope) and r.get("response_received")),None)
            if prior:
                save(self.output/"repeated_requests"/(fingerprint+".json"),{"status":"NO_PROGRESS_IDENTICAL_REQUEST","current_context":context,
                     "previous_request_sha256":prior["request_sha256"],"new_paid_call":False})
                raise RepeatedRequestError("NO_PROGRESS: identical "+purpose+" request already received a reply in this batch; checkpoint retained, no duplicate paid call. Prior request="+prior["request_sha256"])
        replay=None if cache.exists() else self.replay_response(key,purpose,body,context)
        if cache.exists():
            raw=read(cache)
            source="cache"
            print(f"[CACHE] model={key} purpose={display_action} request={fingerprint[:12]}; no new API call",flush=True)
        elif replay is not None:
            raw=replay
            source="verified_history"
            save(cache,raw)
        else:
            source="live"
            start = time.monotonic()
            record = {**context,"purpose":purpose,"api_model_key":key,"model_id":model["id"],
                      "request_sha256":fingerprint,"request_context":context,"request_body_sha256":digest(body),
                      "max_output_tokens_requested":body.get("max_tokens"),"reasoning_requested":body.get("reasoning"),
                      "started_unix":time.time(),"response_received":False}
            try:
                print(f"[LLM] {context.get('unit')} preparing_B={context.get('round')} "
                      f"A_attempt={context.get('step_a_attempt',1)} {display_action} model={key} "
                      f"source=live request={fingerprint[:12]} output_limit={body.get('max_tokens','provider_default')} reasoning={reasoning}",flush=True)
                raw = self.request("/chat/completions",body)
                # Persist even malformed content: the request may already have been billed.
                save(cache,raw)
                usage = raw.get("usage") or {}
                choices=raw.get("choices") or []
                first=choices[0] if choices and isinstance(choices[0],dict) else {}
                prompt,completion = usage.get("prompt_tokens"),usage.get("completion_tokens")
                pricing = self.catalog.get(model["id"],{}).get("pricing",{})
                estimate = None
                if prompt is not None and completion is not None and "prompt" in pricing and "completion" in pricing:
                    estimate = int(prompt)*float(pricing["prompt"])+int(completion)*float(pricing["completion"])+float(pricing.get("request",0) or 0)
                cost = usage.get("cost")
                record.update({"response_received":True,"response_id":raw.get("id"),
                    "finish_reason":first.get("finish_reason"),"native_finish_reason":first.get("native_finish_reason"),
                    "pricing_per_token_snapshot":pricing,
                    "prompt_tokens":prompt,"completion_tokens":completion,
                    "total_tokens":usage.get("total_tokens",(prompt+completion) if prompt is not None and completion is not None else None),
                    "usage_complete":prompt is not None and completion is not None,
                    "cached_prompt_tokens":(usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
                    "reasoning_tokens":(usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                    "reported_cost_usd":float(cost) if cost is not None else None,
                    "estimated_cost_usd":estimate,"raw_usage":usage})
            except Exception as e:
                record["error"] = str(e).replace(self.key,"[REDACTED]")
                raise
            finally:
                record["duration_seconds"]=time.monotonic()-start
                with self.ledger.open("a",encoding="utf-8") as f:
                    f.write(json.dumps(record,ensure_ascii=False)+"\n")
                self.export()
        print(f"[LLM_RESULT] model={key} purpose={display_action} source={source} request={fingerprint[:12]} "
              f"response_id={raw.get('id','unavailable')}",flush=True)
        if raw.get("error"): raise RuntimeError(f"Provider error: {raw['error']}")
        try:
            choice=raw["choices"][0]
            if choice.get("finish_reason")=="length":
                usage=raw.get("usage") or {}
                content=(choice.get("message") or {}).get("content")
                detail={"status":"TRUNCATED_RESPONSE","finish_reason":"length","model_id":model["id"],
                        "request_sha256":fingerprint,"source":source,"max_output_tokens_requested":body.get("max_tokens"),
                        "reasoning_requested":body.get("reasoning"),
                        "completion_tokens":usage.get("completion_tokens"),
                        "reasoning_tokens":(usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                        "visible_content_characters":len(content) if isinstance(content,str) else 0,
                        "raw_response":str(cache),"auto_retry":False,"partial_assertions_accepted":False}
                save(cache.with_suffix(".incomplete.json"),detail)
                print(f"[TRUNCATED] model={key} output_limit={body.get('max_tokens','provider_default')} "
                      f"completion_tokens={detail['completion_tokens']} reasoning_tokens={detail['reasoning_tokens']} "
                      f"visible_chars={detail['visible_content_characters']}; no Step A checks or automatic retry",flush=True)
                raise IncompleteResponseError(f"Incomplete model response (length): output_limit={body.get('max_tokens','provider_default')}, "
                    f"completion_tokens={detail['completion_tokens']}, reasoning_tokens={detail['reasoning_tokens']}; "
                    f"saved at {cache}. Requested reasoning={reasoning}; model/provider did not return a complete answer. "
                    "Raw reply and token/cost ledger retained; no partial assertions accepted or automatic paid retry",str(content or ""),cache)
            if choice.get("finish_reason")=="content_filter":
                raise ModelProtocolError(f"Incomplete model response (content_filter); saved at {cache}; no assertion repair attempted")
            content=choice["message"]["content"]
        except (KeyError,TypeError,IndexError) as error:
            raise ModelProtocolError(f"Missing model response content; inspect {cache}") from error
        notes=[]
        try:
            result=assertion_blocks(content,context.get("repair_ids",()),context.get("review_ids",())) if blocks else json_content(content,notes)
        except (ValueError,TypeError) as error:
            raise ResponseFormatError(str(error),str(content),cache) from error
        if notes:
            save(cache.with_suffix(".parse.json"),{"normalizations":notes,"parsed":result})
            print("[FORMAT] normalized JSON punctuation/comments locally; no extra API call",flush=True)
        return result

    def records(self):
        if not self.ledger.exists(): return []
        return [json.loads(line) for line in self.ledger.read_text().splitlines() if line.strip()]

    def export(self):
        rows=self.records()
        table(self.output/"usage.csv",[{k:v for k,v in r.items() if k!="raw_usage"} for r in rows])
        groups={}
        for r in rows:
            key=(r.get("generator_model"),r["api_model_key"],r["purpose"])
            groups.setdefault(key,[]).append(r)
        summary=[{"generator_model":k[0],"billed_api_model":k[1],"purpose":k[2],**totals(v)} for k,v in groups.items()]
        table(self.output/"cost_by_model.csv",summary)
        by_generator={}
        for r in rows: by_generator.setdefault(r.get("generator_model"),[]).append(r)
        generator_rows=[]
        for key,items in sorted(by_generator.items(),key=lambda item:item[0] or ""):
            gen=[r for r in items if r["purpose"]=="generation" or (r["purpose"]=="format_repair" and r.get("format_repair_for")=="generation")]
            judge=[r for r in items if r["purpose"]=="spec_check" or (r["purpose"]=="format_repair" and r.get("format_repair_for")=="spec_check")]
            generator_rows.append({"generator_model":key,**totals(items),
                "generation_reported_cost_usd":totals(gen)["reported_cost_usd"],
                "gpt_checker_reported_cost_usd":totals(judge)["reported_cost_usd"],
                "generation_estimated_cost_usd":totals(gen)["estimated_cost_usd"],
                "gpt_checker_estimated_cost_usd":totals(judge)["estimated_cost_usd"]})
        table(self.output/"cost_by_generator.csv",generator_rows)
        save(self.output/"cost_summary.json",{"currency":"USD","groups":summary,"total":totals(rows),
             "note":"Reported provider cost preferred. Token-price estimate excludes cache discounts, provider fees and retries with missing usage. Missing fields are explicitly counted, not assumed free."})
