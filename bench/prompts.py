"""Test suite. Each run builds fresh prompts (new nonce + randomized content) so
no provider can serve a cached prefix, while every provider in the same run gets
identical requests.

Each spec: test name, messages, max_tokens, timeout, optional extra body
(response_format / tools), and a `check` function that grades the answer.
"""
import json
import random
import string
import uuid

# ---------------------------------------------------------------- helpers

SERVICES = ["api-gateway", "scheduler", "kubelet", "node-agent", "storage-csi",
            "ingress", "autoscaler", "inference-router", "kv-cache", "metrics"]
LEVELS = ["INFO", "INFO", "INFO", "WARN", "ERROR"]
MESSAGES = [
    "pod evicted due to memory pressure", "image pull completed",
    "GPU health check passed", "NCCL timeout on rank 3, retrying",
    "node joined cluster", "readiness probe failed", "lease renewed",
    "disk pressure threshold crossed", "request queue depth above SLO",
    "scaled replica set", "TLS certificate rotated", "XID 79 reported by driver",
]
TOPICS = [
    "how Kubernetes schedules a pod onto a node",
    "why KV cache size limits LLM serving batch size",
    "what speculative decoding is and when it helps",
    "the difference between tensor and expert parallelism",
    "why time-to-first-token and tokens-per-second trade off",
    "how prefill and decode differ in GPU utilization",
    "what InfiniBand provides over Ethernet for training clusters",
    "why cold starts matter for autoscaled inference",
]
CUSTOMERS = ["Northwind Labs", "Contoso Robotics", "Fabrikam Bio", "Tailspin Media",
             "Adatum Health", "Litware Games", "Proseware AI", "Wingtip Energy"]
REGIONS = ["us-west-2", "us-east-1", "eu-west-1", "ap-northeast-1"]
SEVERITIES = ["low", "medium", "high", "critical"]
GPU_TYPES = ["H100", "H200", "B200", "MI300X"]


def nonce_line() -> str:
    # Leading nonce defeats provider-side prompt/prefix caching.
    return f"Request ID: {uuid.uuid4()}\n\n"


def log_lines(rng: random.Random, n: int) -> list[str]:
    lines = []
    for i in range(n):
        lines.append(
            f"2026-10-{rng.randint(1, 28):02d}T{rng.randint(0, 23):02d}:"
            f"{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}Z "
            f"[{rng.choice(SERVICES)}] {rng.choice(LEVELS)} "
            f"node=aks-gpu-{rng.randint(100, 999)} "
            f'msg="{rng.choice(MESSAGES)}" latency_ms={rng.randint(1, 4000)}'
        )
    return lines


# Roughly 36 tokens per log line for gpt-oss tokenization.
TOKENS_PER_LOG_LINE = 36


# ---------------------------------------------------------------- tests

def short_chat(rng):
    topic = rng.choice(TOPICS)
    return {
        "test": "short",
        "messages": [{"role": "user", "content": nonce_line() +
                      f"Explain {topic} to a new infrastructure engineer in about 200 words."}],
        "max_tokens": 1200,
        "timeout": 90,
        "check": lambda text, tools: {"check_pass": len(text.split()) >= 50},
    }


def medium_chat(rng):
    logs = "\n".join(log_lines(rng, 2000 // TOKENS_PER_LOG_LINE))
    return {
        "test": "medium",
        "messages": [{"role": "user", "content": nonce_line() +
                      "Here are cluster logs:\n\n" + logs +
                      "\n\nSummarize the notable incidents in about 400 words, grouped by service."}],
        "max_tokens": 4096,
        "timeout": 120,
        "check": lambda text, tools: {"check_pass": len(text.split()) >= 100},
    }


TICKET_SCHEMA = {
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


def structured(rng):
    truth = {
        "customer": rng.choice(CUSTOMERS),
        "severity": rng.choice(SEVERITIES),
        "region": rng.choice(REGIONS),
        "gpu_count": rng.choice([8, 16, 32, 64, 128, 256]),
    }
    ticket = (
        f"Hi team, this is the on-call lead at {truth['customer']}. Since this morning "
        f"our training job in {truth['region']} has lost access to {truth['gpu_count']} GPUs "
        f"after a maintenance event. We'd classify this as {truth['severity']} severity. "
        f"Please advise on next steps."
    )

    def check(text, tools):
        # check_pass is strict: the whole response must be one schema-valid object
        # with correct values (that's what json_schema mode promises). If the strict
        # parse fails, also try the leading JSON object, so the analysis can separate
        # "format not enforced" from "wrong values".
        strict = True
        try:
            obj = json.loads(text)
        except Exception:
            strict = False
            try:
                obj, _ = json.JSONDecoder().raw_decode(text.lstrip())
            except Exception:
                return {"check_pass": False, "json_strict": False, "json_extractable": False,
                        "schema_valid": False, "check_detail": "not JSON"}
        if not isinstance(obj, dict):
            return {"check_pass": False, "json_strict": strict, "json_extractable": True,
                    "schema_valid": False, "check_detail": "not an object"}
        try:
            import jsonschema
            jsonschema.validate(obj, TICKET_SCHEMA)
            valid = True
        except Exception:
            valid = False
        correct = all(obj.get(k) == v for k, v in truth.items())
        out = {"check_pass": strict and valid and correct, "json_strict": strict,
               "json_extractable": True, "schema_valid": valid, "fields_correct": correct}
        if not strict:
            out["check_detail"] = "extra text after JSON object"
        return out

    return {
        "test": "structured",
        "messages": [{"role": "user", "content": nonce_line() +
                      "Extract the support ticket fields as JSON.\n\nTicket:\n" + ticket}],
        "max_tokens": 1200,
        "timeout": 90,
        "body": {"response_format": {"type": "json_schema", "json_schema": {
            "name": "ticket", "strict": True, "schema": TICKET_SCHEMA}}},
        "check": check,
    }


SCALE_TOOL = {
    "type": "function",
    "function": {
        "name": "scale_node_pool",
        "description": "Scale a GPU node pool in a Kubernetes cluster to a target node count.",
        "parameters": {
            "type": "object",
            "properties": {
                "cluster": {"type": "string"},
                "node_pool": {"type": "string"},
                "count": {"type": "integer"},
                "gpu_type": {"type": "string", "enum": GPU_TYPES},
            },
            "required": ["cluster", "node_pool", "count", "gpu_type"],
        },
    },
}
DECOY_TOOL = {
    "type": "function",
    "function": {
        "name": "get_cluster_status",
        "description": "Return health and utilization for a cluster.",
        "parameters": {"type": "object", "properties": {"cluster": {"type": "string"}},
                       "required": ["cluster"]},
    },
}


def tool_call(rng):
    truth = {
        "cluster": f"prod-{rng.choice(['west', 'east', 'eu'])}-{rng.randint(1, 9)}",
        "node_pool": f"{rng.choice(['infer', 'train', 'batch'])}pool{rng.randint(1, 4)}",
        "count": rng.choice([2, 4, 6, 10, 16, 24]),
        "gpu_type": rng.choice(GPU_TYPES),
    }

    def check(text, tools):
        if not tools:
            return {"check_pass": False, "tool_called": False}
        call = tools[0]
        name_ok = call.get("name") == "scale_node_pool"
        try:
            args = json.loads(call.get("arguments") or "{}")
        except Exception:
            args = {}
        args_ok = all(args.get(k) == v for k, v in truth.items())
        return {"check_pass": name_ok and args_ok, "tool_called": True,
                "tool_name_ok": name_ok, "tool_args_ok": args_ok}

    return {
        "test": "tool",
        "messages": [{"role": "user", "content": nonce_line() +
                      f"Scale the {truth['node_pool']} node pool on cluster {truth['cluster']} "
                      f"to {truth['count']} {truth['gpu_type']} nodes."}],
        "max_tokens": 1200,
        "timeout": 90,
        "body": {"tools": [DECOY_TOOL, SCALE_TOOL], "tool_choice": "auto"},
        "check": check,
    }


def long_context(rng):
    n = 32000 // TOKENS_PER_LOG_LINE
    lines = log_lines(rng, n)
    cluster = f"prod-{rng.choice(['west', 'east', 'eu'])}-{rng.randint(10, 99)}"
    code = "RB-" + "".join(rng.choices(string.ascii_uppercase + string.digits, k=6))
    depth = rng.random()  # where in the context the needle sits
    lines.insert(int(depth * len(lines)),
                 f"NOTE: the rollback code for cluster {cluster} is {code}.")
    return {
        "test": "long",
        "messages": [{"role": "user", "content": nonce_line() +
                      "Here are cluster logs:\n\n" + "\n".join(lines) +
                      f"\n\nWhat is the rollback code for cluster {cluster}? "
                      "Reply with only the code."}],
        "max_tokens": 1500,
        "timeout": 180,
        "needle_depth": round(depth, 3),
        "check": lambda text, tools: {"check_pass": code in text},
    }


# How many of each test per provider per run.
SUITE = [(short_chat, 5), (medium_chat, 3), (structured, 3), (tool_call, 3)]
LONG = (long_context, 2)


def build_tests(include_long: bool, seed: int | None = None) -> list[dict]:
    rng = random.Random(seed)
    specs = []
    plan = SUITE + ([LONG] if include_long else [])
    for fn, count in plan:
        for _ in range(count):
            specs.append(fn(rng))
    return specs
