#!/usr/bin/env python3
"""Standalone repro of the structured-output bug, independent of the benchmark
harness.

Each request is a support ticket with randomized fields (customer, severity,
region, and a GPU count from 8 to 256, as in the benchmark). The script sends it
in several configurations so no single client choice can explain the result:

  with_prompt_*  schema also given in a system prompt (as Together's docs recommend)
  no_prompt_*    schema only in response_format, the way the benchmark sent it

Usage:
  export TOGETHER_API_KEY=...
  python bench/repro_structured.py                          # Together, 50 per config, ~200 requests
  python bench/repro_structured.py --n 100 --only with_prompt_stream,no_prompt_stream
  export DEEPINFRA_API_KEY=...
  python bench/repro_structured.py --provider deepinfra
  python bench/repro_structured.py --provider together,deepinfra
  python bench/repro_structured.py --n 20 --save outputs.jsonl   # full raw responses to share
"""
import argparse
import json
import os
import random
import uuid

import httpx

PROVIDERS = {
    "together":  dict(base_url="https://api.together.xyz/v1",
                      model="openai/gpt-oss-120b", key_env="TOGETHER_API_KEY"),
    "deepinfra": dict(base_url="https://api.deepinfra.com/v1/openai",
                      model="openai/gpt-oss-120b", key_env="DEEPINFRA_API_KEY"),
    "fireworks": dict(base_url="https://api.fireworks.ai/inference/v1",
                      model="accounts/fireworks/models/gpt-oss-120b", key_env="FIREWORKS_API_KEY"),
}

CUSTOMERS = ["Northwind Labs", "Contoso Robotics", "Fabrikam Bio", "Tailspin Media",
             "Adatum Health", "Litware Games", "Proseware AI", "Wingtip Energy"]
SEVERITIES = ["low", "medium", "high", "critical"]
REGIONS = ["us-west-2", "us-east-1", "eu-west-1", "ap-northeast-1"]
GPU_COUNTS = [8, 16, 32, 64, 128, 256]

SCHEMA = {
    "type": "object",
    "properties": {
        "customer": {"type": "string"},
        "severity": {"type": "string", "enum": SEVERITIES},
        "region": {"type": "string", "enum": REGIONS},
        "gpu_count": {"type": "integer"},
    },
    "required": ["customer", "severity", "region", "gpu_count"],
    "additionalProperties": False,
}

# All use strict json_schema, temperature 0, reasoning_effort medium.
CONFIGS = {
    "with_prompt_stream":    dict(stream=True,  sys=True),
    "with_prompt_nonstream": dict(stream=False, sys=True),
    "no_prompt_stream":      dict(stream=True,  sys=False),   # exactly what the benchmark sent
    "no_prompt_nonstream":   dict(stream=False, sys=False),
}


def make_ticket(rng):
    truth = {"customer": rng.choice(CUSTOMERS), "severity": rng.choice(SEVERITIES),
             "region": rng.choice(REGIONS), "gpu_count": rng.choice(GPU_COUNTS)}
    text = (f"Hi team, this is the on-call lead at {truth['customer']}. Since this morning "
            f"our training job in {truth['region']} has lost access to {truth['gpu_count']} GPUs "
            f"after a maintenance event. We'd classify this as {truth['severity']} severity. "
            f"Please advise on next steps.")
    return truth, text


def build(model, cfg, ticket_text):
    messages = []
    if cfg["sys"]:
        messages.append({"role": "system", "content": "Respond only in JSON matching this schema:\n"
                                                      + json.dumps(SCHEMA)})
    messages.append({"role": "user", "content": f"Request ID: {uuid.uuid4()}\n\n"
                     "Extract the support ticket fields as JSON.\n\nTicket:\n" + ticket_text})
    body = {
        "model": model,
        "temperature": 0,
        "max_tokens": 1200,
        "reasoning_effort": "medium",
        "messages": messages,
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "ticket", "strict": True, "schema": SCHEMA}},
    }
    if cfg["stream"]:
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
    return body


def call(client, url, headers, body):
    """Returns (answer text, raw response): the full JSON body, or every stream event."""
    if not body.get("stream"):
        r = client.post(url, json=body, headers=headers)
        r.raise_for_status()
        raw = r.json()
        return raw["choices"][0]["message"].get("content") or "", raw
    parts, events = [], []
    with client.stream("POST", url, json=body, headers=headers) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if line.startswith("data:") and line[5:].strip() != "[DONE]":
                evt = json.loads(line[5:])
                events.append(evt)
                for ch in evt.get("choices") or []:
                    parts.append((ch.get("delta") or {}).get("content") or "")
    return "".join(parts), events


def grade(text, truth):
    """ok | wrong_value | extra_text | unparseable, plus the returned gpu_count."""
    try:
        obj = json.loads(text)
        strict_ok = True
    except Exception:
        strict_ok = False
        try:
            obj, _ = json.JSONDecoder().raw_decode(text.lstrip())
        except Exception:
            return "unparseable", None
    if not isinstance(obj, dict):
        return "unparseable", None
    values_ok = all(obj.get(k) == v for k, v in truth.items())
    verdict = "wrong_value" if not values_ok else ("ok" if strict_ok else "extra_text")
    return verdict, obj.get("gpu_count")


def run_provider(name, prov, configs, n, seed, save=None):
    key = os.environ.get(prov["key_env"])
    if not key:
        print(f"\n{name}: {prov['key_env']} not set, skipping")
        return
    url = prov["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {key}"}
    print(f"\n=== {name} ({prov['model']}) ===")
    print(f"{'config':<23} {'ok':>4} {'wrong':>6} {'extra':>6} {'other':>6}   wrong values by true gpu_count")
    with httpx.Client(timeout=120) as client:
        for cname, cfg in configs.items():
            rng = random.Random(seed)  # same tickets for every config and provider
            tally = {"ok": 0, "wrong_value": 0, "extra_text": 0, "other": 0}
            wrong_by_count, examples = {}, []
            for _ in range(n):
                truth, text = make_ticket(rng)
                body = build(prov["model"], cfg, text)
                raw = None
                try:
                    out, raw = call(client, url, headers, body)
                    verdict, got = grade(out, truth)
                except Exception as e:
                    verdict, got, out = f"error:{type(e).__name__}", None, str(e)
                if save:
                    resp_id = (raw.get("id") if isinstance(raw, dict)
                               else next((e.get("id") for e in raw or [] if e.get("id")), None))
                    save.write(json.dumps({"provider": name, "config": cname, "verdict": verdict,
                                           "expected": truth, "returned_gpu_count": got,
                                           "response_id": resp_id, "request": body,
                                           "content": out, "raw_response": raw}) + "\n")
                if verdict in tally:
                    tally[verdict] += 1
                else:
                    tally["other"] += 1
                if verdict == "wrong_value":
                    wrong_by_count.setdefault(truth["gpu_count"], []).append(got)
                if verdict != "ok" and len(examples) < 2:
                    examples.append(f"(true {truth['gpu_count']}) " + " ".join(out.split())[:140])
            by = ", ".join(f"{k}->{sorted(set(map(str, v)))}" for k, v in sorted(wrong_by_count.items()))
            print(f"{cname:<23} {tally['ok']:>4} {tally['wrong_value']:>6} {tally['extra_text']:>6} "
                  f"{tally['other']:>6}   {by or '-'}")
            for ex in examples:
                print(f"    e.g. {ex}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="together",
                    help=f"comma-separated: {','.join(PROVIDERS)}")
    ap.add_argument("--n", type=int, default=50, help="requests per config")
    ap.add_argument("--only", help="comma-separated config names: " + ",".join(CONFIGS))
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--save", help="write every request, full response and verdict to this JSONL file")
    args = ap.parse_args()

    configs = {k: v for k, v in CONFIGS.items() if not args.only or k in args.only.split(",")}
    save = open(args.save, "w") if args.save else None
    for name in args.provider.split(","):
        run_provider(name, PROVIDERS[name], configs, args.n, args.seed, save)
    if save:
        save.close()
        print(f"\nsaved full requests and responses to {args.save} (API keys are never written)")


if __name__ == "__main__":
    main()
