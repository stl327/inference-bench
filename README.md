# inference-bench

A week-long benchmark of six serverless inference providers serving the same open-weight model, **gpt-oss-120b**. From October 26 to November 1, 2026, a GitHub Actions job sent an identical set of requests to each provider every 30 minutes and logged latency, throughput, errors, correctness and cost.

**Providers:** Together, Fireworks, Baseten, DeepInfra, Cerebras, and OpenRouter (pinned to Together with fallbacks off, to measure routing overhead).

**Scale:** 338 runs, 29,064 requests (4,844 per provider), $17.68 total API spend.

The study is finished; the schedule and API keys are gone. Everything needed to reproduce it is in this repo.

## Results

### Structured output: the headline finding

The same JSON-schema request (`response_format: json_schema`, `strict: true`, `additionalProperties: false`), about 1,000 times per provider:

| Provider | Correct | Wrong values | Text after the JSON object |
|---|---|---|---|
| Fireworks | 100% | 0 / 1,014 | 0 |
| Baseten | 100% | 0 / 1,007 | 0 |
| Cerebras | 100% | 0 / 1,008 | 0 |
| DeepInfra | 63% | 270 / 910 (30%) | 7% |
| Together | 16% | 135 / 1,006 (13%) | 72% |
| OpenRouter → Together | 20% | 123 / 1,008 (12%) | 70% |

Every wrong value had the same shape: the correct digits followed by extra zeros (8 → 80, 16 → 1600, 64 → 640, 128 → 1280, 256 → 2560). The other fields were always right, and the output still passes schema validation, so an application can't detect the error.

A follow-up repro ([`bench/repro_structured.py`](bench/repro_structured.py)) isolated the trigger. On these two endpoints, the schema is only reliable when it's also described in the prompt, which Together's docs recommend:

| Provider | Schema also in system prompt | Schema only in `response_format` |
|---|---|---|
| Together | 100 / 100 correct | 9 / 100 correct |
| DeepInfra | 52 / 52 correct | 35 / 62 correct |

Results were the same with and without streaming. Fireworks, Baseten and Cerebras returned correct output without the prompt hint. Both affected providers were sent these findings before publication.

Tool calling was 100% correct on every provider, and the 32K-token needle test was 100% on every provider except Fireworks (99%, one miss).

### Speed and cost

Short test, ~4,800 requests per provider. "Answer" is the time to the first visible answer token, after any reasoning.

| Provider | Answer p50 | Answer p99 | Output tok/s (p50) | Effective $ / M tokens |
|---|---|---|---|---|
| Cerebras | 0.25 s | 1.6 s | 1,591 | 0.48 |
| Fireworks | 0.47 s | 1.5 s | 240 | 0.29 |
| Baseten | 0.51 s | 1.4 s | 197 | 0.22 |
| Together | 0.82 s | 2.6 s | 101 | 0.30 |
| OpenRouter → Together | 0.87 s | 2.8 s | 100 | 0.30 |
| DeepInfra | 2.77 s | 10.6 s | 42 | 0.19 |

### Reliability

No retries, so every failed request counts. "Runs hit" is the number of runs (of 338) with at least one failed request.

| Provider | Error rate | Runs hit | Main failure mode |
|---|---|---|---|
| Fireworks | 0.00% | 0 | none |
| Baseten | 0.56% | 3 | bursts of 5xx and 429 |
| Cerebras | 0.60% | 7 | "high traffic" 429s |
| OpenRouter → Together | 0.68% | 8 | inherited Together's outages |
| Together | 0.76% | 8 | one full outage (14 of 14 failed) |
| DeepInfra | 9.93% | 99 | "Model busy" 429s (`engine_overloaded`) |

Load was about one request at a time per provider, so these reflect provider capacity, not account rate limits.

### Load: weekday vs. weekend

Median output tok/s on the short test during US business hours (8 AM–4 PM Pacific):

| Provider | Weekday | Weekend | Change |
|---|---|---|---|
| Together | 89 | 127 | +43% |
| Fireworks | 205 | 252 | +23% |
| Baseten | 162 | 198 | +22% |
| DeepInfra | 37 | 39 | +5% |
| Cerebras | 1,587 | 1,573 | −1% |

This rests on a single weekend (which included Halloween), so treat it as directional.

Full tables, including percentiles per test, errors by type and TTFT by hour, are in [`results-summary/`](results-summary/summary.md).

## Caveats

- One model (gpt-oss-120b, reasoning effort medium, temperature 0). Other models may behave differently on the same providers.
- One vantage point: GitHub-hosted US runners. Connection setup was 70–170 ms for every provider, so network time doesn't explain the differences.
- Low request rate. This measures what a single client sees, not throughput under heavy load.
- Prices in `bench/providers.yaml` were the listed prices at the time; effective $/M is computed from them.
- A personal project, not affiliated with or funded by any provider.

## How it worked

Each run, every provider received the same requests in parallel. Within a provider, requests ran one at a time, so the benchmark never queued its own traffic. Every prompt started with a random request ID, so no provider could serve a cached prefix.

| Test | Shape | Per run | Graded on |
|---|---|---|---|
| short | ~60-token prompt → ~200-word answer | 5 | non-empty answer |
| medium | ~2K-token log dump → ~400-word summary | 3 | non-empty answer |
| structured | ticket text → JSON schema | 3 | schema-valid **and** all 4 fields correct |
| tool | natural-language command, 2 tools offered | 3 | right tool **and** exact args |
| long | ~32K-token log dump with a hidden "needle" | 2, every 6th run | needle code returned |

Runs were triggered every 30 minutes by cron-job.org calling GitHub's workflow-dispatch API, since GitHub's own scheduler drifts by hours. Each run committed its results to the `data` branch as one JSONL file.

### What each row records

- **Latency:** `ttft_s` (first token of any kind, including reasoning), `ttfa_s` (first visible answer token), `tpot_ms`, `output_tps`, `e2e_s`
- **Tokens and cost:** input, output and reasoning tokens from the provider's `usage`, and `cost_usd` from `providers.yaml`
- **Errors:** `rate_limited`, `billing`, `auth`, `server_error`, `client_error`, `timeout`, `network`, `stream_error`, `empty_output`, plus `truncated`
- **Correctness:** `check_pass`, plus per-test detail (`json_strict`, `schema_valid`, `fields_correct`, `tool_name_ok`, `tool_args_ok`)
- **Network:** `tcp_ms` / `tls_ms` from a bare handshake per provider per run
- **Routing:** `upstream`, the provider OpenRouter actually used (Together for every request)

### Repo layout

```
bench/run.py                  one benchmark pass → results/<date>/<time>_<run>.jsonl
bench/prompts.py              the test suite
bench/providers.yaml          endpoints, model IDs and prices
bench/analyze.py              DuckDB analysis → summary.md + CSVs
bench/repro_structured.py     standalone structured-output repro
.github/workflows/bench.yml   the scheduled job
results-summary/              final tables for the study window
data branch                   raw results, one JSONL per run
```

## Reproducing

**Re-run the analysis** on the raw data:

```bash
pip install -r requirements.txt -r requirements-analysis.txt
git fetch origin data && git worktree add data origin/data
python bench/analyze.py --results data/results --out analysis --since 2026-10-26T04:00
```

`--since` excludes six setup runs from before the study started.

**Reproduce the structured-output finding** (a few cents per provider):

```bash
export TOGETHER_API_KEY=...       # and/or DEEPINFRA_API_KEY, FIREWORKS_API_KEY
python bench/repro_structured.py --provider together --n 50
python bench/repro_structured.py --provider together,deepinfra,fireworks --n 20 --save outputs.jsonl
```

**Run the benchmark yourself:**

1. Fork the repo and create a `data` branch:
   ```bash
   git checkout --orphan data
   git rm -rf . && mkdir results && touch results/.keep
   git add results && git commit -m "init data branch" && git push origin data
   git checkout main
   ```
2. Add repository secrets for the providers you want: `TOGETHER_API_KEY`, `FIREWORKS_API_KEY`, `BASETEN_API_KEY`, `DEEPINFRA_API_KEY`, `CEREBRAS_API_KEY`, `OPENROUTER_API_KEY`. Providers without a key are skipped. Optionally add `NTFY_TOPIC` for phone alerts on outages and auth or billing failures.
3. Check the model IDs and prices in `bench/providers.yaml`.
4. Run the workflow once from the Actions tab, then schedule it. Any scheduler that can call the [workflow-dispatch API](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event) works.

A week costs about $2–5 per provider at the listed prices, and each run takes 2–5 minutes of Actions time, depending on the slowest provider.
