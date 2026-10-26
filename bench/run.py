#!/usr/bin/env python3
"""One benchmark pass: every provider runs the same request set in parallel,
requests within a provider run one at a time (no self-inflicted queueing).
Writes one JSONL row per request.

Usage:
  python bench/run.py                      # normal run
  python bench/run.py --long               # force the long-context test
  python bench/run.py --only together,cerebras --tests short,tool
"""
import argparse
import asyncio
import json
import os
import socket
import ssl
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prompts import build_tests  # noqa: E402


# ------------------------------------------------------------ network probe

def probe_connect(base_url: str) -> dict:
    """Bare TCP + TLS handshake time, so network can be separated from server time."""
    u = urlparse(base_url)
    if u.scheme != "https":
        return {}
    host, port = u.hostname, u.port or 443
    try:
        t0 = time.perf_counter()
        sock = socket.create_connection((host, port), timeout=10)
        t1 = time.perf_counter()
        tls = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        t2 = time.perf_counter()
        tls.close()
        return {"tcp_ms": round((t1 - t0) * 1000, 1), "tls_ms": round((t2 - t1) * 1000, 1)}
    except Exception as e:  # recorded, never fatal
        return {"probe_error": f"{type(e).__name__}: {e}"[:200]}


# ------------------------------------------------------------ one request

def classify_status(code: int) -> str:
    if code == 429:
        return "rate_limited"
    if code == 402:
        return "billing"
    if code in (401, 403):
        return "auth"
    if code >= 500:
        return "server_error"
    return "client_error"


async def one_request(name: str, prov: dict, spec: dict, meta: dict) -> dict:
    url = prov["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {os.environ[prov['key_env']]}",
               "Content-Type": "application/json"}
    body = {
        "model": prov["model"],
        "messages": spec["messages"],
        "max_tokens": spec["max_tokens"],
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    body.update(spec.get("body", {}))
    body.update(prov.get("extra", {}))

    row = {
        **meta,
        "provider": name,
        "model": prov["model"],
        "test": spec["test"],
        "request_id": str(uuid.uuid4()),
        "ts_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "needle_depth": spec.get("needle_depth"),
    }

    t0 = time.perf_counter()
    first_any = first_answer = last_tok = None
    text_parts, tools = [], {}
    usage, finish, chunks = None, None, 0
    status, error, error_detail = None, None, None

    try:
        timeout = httpx.Timeout(spec["timeout"], connect=15)
        # Fresh client per request: every request pays the same connection setup,
        # so providers are compared on equal footing.
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, json=body, headers=headers) as r:
                status = r.status_code
                if status != 200:
                    error = classify_status(status)
                    error_detail = (await r.aread()).decode(errors="replace")[:400]
                else:
                    async for line in r.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            evt = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if evt.get("error"):
                            error = "stream_error"
                            error_detail = json.dumps(evt["error"])[:400]
                            break
                        if evt.get("usage"):
                            usage = evt["usage"]
                        for ch in evt.get("choices") or []:
                            d = ch.get("delta") or {}
                            now = time.perf_counter()
                            reasoning = d.get("reasoning_content") or d.get("reasoning")
                            reasoning = reasoning if isinstance(reasoning, str) else None
                            content = d.get("content")
                            tcs = d.get("tool_calls")
                            if reasoning or content or tcs:
                                chunks += 1
                                last_tok = now
                                first_any = first_any or now
                            if content or tcs:
                                first_answer = first_answer or now
                            if content:
                                text_parts.append(content)
                            for tc in tcs or []:
                                slot = tools.setdefault(tc.get("index", 0),
                                                        {"name": "", "arguments": ""})
                                fn = tc.get("function") or {}
                                slot["name"] += fn.get("name") or ""
                                slot["arguments"] += fn.get("arguments") or ""
                            if ch.get("finish_reason"):
                                finish = ch["finish_reason"]
    except httpx.TimeoutException:
        error = "timeout"
    except httpx.TransportError as e:
        error, error_detail = "network", f"{type(e).__name__}: {e}"[:400]
    except Exception as e:
        error, error_detail = "harness", f"{type(e).__name__}: {e}"[:400]

    t_end = time.perf_counter()
    text = "".join(text_parts).strip()
    tool_list = [tools[k] for k in sorted(tools)]

    if error is None and not text and not tool_list:
        error = "empty_output"

    usage = usage or {}
    in_tok = usage.get("prompt_tokens")
    out_tok = usage.get("completion_tokens") or (chunks or None)
    reasoning_tok = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
    decode_s = (last_tok - first_any) if (first_any and last_tok) else None

    row.update({
        "http_status": status,
        "error": error,
        "error_detail": error_detail,
        "finish_reason": finish,
        "truncated": finish == "length",
        "ttft_s": round(first_any - t0, 4) if first_any else None,      # first token of any kind
        "ttfa_s": round(first_answer - t0, 4) if first_answer else None,  # first visible answer token
        "e2e_s": round(t_end - t0, 4),
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "reasoning_tokens": reasoning_tok,
        "usage_reported": bool(usage),
        "tpot_ms": round(decode_s / (out_tok - 1) * 1000, 3)
                   if (decode_s and out_tok and out_tok > 1) else None,
        "output_tps": round(out_tok / decode_s, 1) if (decode_s and out_tok) else None,
        "cost_usd": round((in_tok or 0) / 1e6 * prov["price_in"] +
                          (out_tok or 0) / 1e6 * prov["price_out"], 6) if usage else None,
    })

    if error is None:
        try:
            row.update(spec["check"](text, tool_list))
        except Exception as e:
            row.update({"check_pass": False, "check_detail": f"checker error: {e}"[:200]})
    row["answer_preview"] = text[:200]
    return row


async def run_provider(name, prov, specs, meta):
    probe = await asyncio.to_thread(probe_connect, prov["base_url"])
    rows = []
    for spec in specs:  # sequential within a provider
        row = await one_request(name, prov, spec, meta)
        row.update(probe)
        rows.append(row)
    return rows


# ------------------------------------------------------------ main

def long_due(now: datetime) -> bool:
    # cron-job.org fires at :00 and :30 UTC-aligned half hours. The :00 run in every
    # 3rd UTC hour includes long context → 8 long runs/day (1 in 6 runs).
    return now.hour % 3 == 0 and now.minute < 30


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--providers-file", default=str(Path(__file__).with_name("providers.yaml")))
    ap.add_argument("--out-dir", default="results")
    ap.add_argument("--only", help="comma-separated provider names")
    ap.add_argument("--tests", help="comma-separated test names (short,medium,structured,tool,long)")
    ap.add_argument("--long", action="store_true", help="force the long-context test")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.providers_file).read_text())
    providers = cfg["providers"]
    if args.only:
        wanted = set(args.only.split(","))
        providers = {k: v for k, v in providers.items() if k in wanted}

    missing = [k for k, v in providers.items() if not os.environ.get(v["key_env"])]
    for k in missing:
        print(f"skip {k}: {providers[k]['key_env']} not set", file=sys.stderr)
    providers = {k: v for k, v in providers.items() if k not in missing}
    if not providers:
        sys.exit("no providers with API keys set")

    now = datetime.now(timezone.utc)
    include_long = args.long or long_due(now) or (args.tests and "long" in args.tests)
    specs = build_tests(include_long=bool(include_long))
    if args.tests:
        keep = set(args.tests.split(","))
        specs = [s for s in specs if s["test"] in keep]

    meta = {
        "run_id": os.environ.get("GITHUB_RUN_ID") or uuid.uuid4().hex[:12],
        "run_started_utc": now.isoformat(timespec="seconds"),
        "runner": os.environ.get("RUNNER_NAME", socket.gethostname()),
        "model_label": cfg.get("model_label"),
    }

    t0 = time.perf_counter()
    results = await asyncio.gather(*(run_provider(k, v, specs, meta)
                                     for k, v in providers.items()))
    rows = [r for group in results for r in group]

    out = Path(args.out_dir) / now.strftime("%Y-%m-%d")
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{now.strftime('%H%M%S')}_{meta['run_id']}.jsonl"
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    # Console summary
    print(f"\nwrote {len(rows)} rows to {path} in {time.perf_counter() - t0:.1f}s "
          f"(long={'yes' if include_long else 'no'})\n")
    hdr = f"{'provider':<11} {'ok':>5} {'ttft p50':>9} {'tok/s p50':>10} {'checks':>7} {'reason tok':>10} {'cost $':>8}"
    print(hdr)
    print("-" * len(hdr))
    for name in providers:
        pr = [r for r in rows if r["provider"] == name]
        ok = [r for r in pr if not r["error"]]
        med = lambda xs: sorted(xs)[len(xs) // 2] if xs else None  # noqa: E731
        ttft = med([r["ttft_s"] for r in ok if r["ttft_s"]])
        tps = med([r["output_tps"] for r in ok if r["output_tps"]])
        checks = sum(1 for r in ok if r.get("check_pass"))
        rtok = med([r["reasoning_tokens"] for r in ok if r.get("reasoning_tokens")])
        cost = sum(r["cost_usd"] or 0 for r in pr)
        print(f"{name:<11} {len(ok):>2}/{len(pr):<2} "
              f"{(f'{ttft:.2f}s' if ttft else '-'):>9} {(f'{tps:.0f}' if tps else '-'):>10} "
              f"{checks:>3}/{len(ok):<3} {(str(rtok) if rtok else '-'):>10} {cost:>8.4f}")
    # Errors: one line per provider + error type, with a count and a flattened sample.
    seen = {}
    for r in rows:
        if r["error"]:
            key = (r["provider"], r["error"], r["http_status"])
            seen.setdefault(key, [0, r])[0] += 1
    for (prov, err, code), (n, r) in seen.items():
        detail = " ".join((r["error_detail"] or "").split())[:160]
        print(f"  ! {prov}: {n}x {err} {code or ''} {detail}")
    # Failed correctness checks, so harness bugs vs. real provider misses are visible.
    for r in rows:
        if not r["error"] and not r.get("check_pass"):
            flags = {k: r[k] for k in ("schema_valid", "fields_correct", "tool_called",
                                       "tool_name_ok", "tool_args_ok", "check_detail")
                     if k in r}
            preview = " ".join((r.get("answer_preview") or "").split())[:100]
            print(f"  x {r['provider']}/{r['test']}: finish={r['finish_reason']} {flags} {preview!r}")


if __name__ == "__main__":
    asyncio.run(main())
