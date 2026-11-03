# inference-bench

A week-long benchmark of six inference endpoints serving the same open-weight model, **gpt-oss-120b**: five serverless providers, plus OpenRouter pinned to one of them. From October 25 to November 1, 2026 (Pacific time; October 26 04:00 to November 2 04:35 UTC), a GitHub Actions job sent an identical set of requests to each endpoint every 30 minutes and logged latency, throughput, errors, correctness and cost.

**Providers:** Together, Fireworks, Baseten, DeepInfra, Cerebras, and OpenRouter (pinned to Together with fallbacks off, to measure routing overhead).

**Scale:** 338 runs, 29,064 requests (4,844 per endpoint), about $18 in estimated API usage ($17.68, computed from recorded token usage and the listed prices in `bench/providers.yaml`).

The study is finished; the schedule and API keys are gone. Everything needed to reproduce it is in this repo.

## Results

### Structured output: the headline finding

The same extraction request about 1,000 times per endpoint. The schema was supplied only through `response_format` (`json_schema`, `strict: true`, `additionalProperties: false`), with no schema text in the prompt. "Correct" means a single JSON object that passes the schema with every field matching the ticket; requests that returned API errors are excluded.

| Endpoint | Correct | Wrong values | Not a single valid JSON object |
|---|---|---|---|
| Fireworks | 100% | 0 / 1,014 | 0 |
| Baseten | 100% | 0 / 1,007 | 0 |
| Cerebras | 100% | 0 / 1,008 | 0 |
| DeepInfra | 63% | 270 / 910 (30%) | 7% |
| Together | 16% | 135 / 1,006 (13%) | 72% |
| OpenRouter → Together | 19% | 123 / 1,008 (12%) | 70% |

Almost every wrong value had the same shape: the correct digits followed by extra zeros (8 → 80, 16 → 1600, 64 → 640, 128 → 1280, 256 → 2560). The one exception was a DeepInfra response that returned 16016 for 16. Of the wrong-value responses, 270 of 270 (DeepInfra), 122 of 135 (Together) and 107 of 123 (OpenRouter) were otherwise valid, schema-conforming JSON, so schema validation alone would not have caught them: 1280 is a valid integer even when the ticket says 128.

A follow-up repro ([`bench/repro_structured.py`](bench/repro_structured.py)) tested the same request with and without the schema also written into a system prompt, which [Together's docs](https://docs.together.ai/docs/inference/chat/structured-outputs) recommend. Adding it eliminated the failures in these follow-up tests. Two runs:

| Run | Provider | Schema also in system prompt | Schema only in `response_format` | Evidence |
|---|---|---|---|---|
| 1 | Together | 100 / 100 correct | 9 / 100 correct | [terminal output](results-summary/repro/run1_terminal_output.png) only |
| 1 | DeepInfra | 52 / 52 correct | 35 / 62 correct | [terminal output](results-summary/repro/run1_terminal_output.png) only |
| 2 | Together | 40 / 40 correct | 7 / 40 correct | [full requests and responses](results-summary/repro/run2_together_outputs.jsonl), with Together response IDs |

Run 1 was printed to the terminal without saving raw responses, so only its summary output is available; run 2 saved everything. DeepInfra's counts exclude requests rejected with 429. Both runs graded JSON parsing and extracted values; the script now also runs full JSON Schema validation. Results were the same with and without streaming. Fireworks, Baseten and Cerebras returned correct output without the prompt hint. Both affected providers were sent these findings before publication.

Tool calling was 100% correct on every endpoint, and the 32K-token needle test was 100% on every endpoint except Fireworks (99%, one miss).

### Speed and cost

Short test, 1,690 requests per endpoint. "Answer" is the time to the first visible answer token, after any reasoning. Output tok/s counts all completion tokens the provider reports, including reasoning, over the generation time; it is not the same as total response time. Effective $/M is cost per million input plus output tokens across all tests.

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
| DeepInfra | 9.93% | 99 | 461 "Model busy" 429s (`engine_overloaded`), 20 empty responses |

Error rates cover API and request errors across all tests; they don't include wrong answers. Load was about one request at a time per endpoint, so the 429s reflect provider capacity rather than account rate limits.

### Load: weekday vs. weekend

Median output tok/s on successful short-test requests between 8 AM and 4 PM Pacific (America/Los_Angeles, so it follows the November 1 daylight-saving change):

| Provider | Weekday | Weekend | Change |
|---|---|---|---|
| Together | 89 | 127 | +42% |
| Fireworks | 203 | 252 | +25% |
| Baseten | 160 | 195 | +22% |
| DeepInfra | 37 | 38 | +3% |
| Cerebras | 1,582 | 1,562 | −1% |

Cerebras's weekday and weekend medians were nearly the same, which doesn't mean its speed was constant through the week. This rests on a single weekend (which included Halloween), so treat it as directional.

Full tables, including percentiles per test, errors by type and TTFT by hour, are in [`results-summary/`](results-summary/summary.md).

## Caveats

- One model (gpt-oss-120b, reasoning effort medium, temperature 0). Other models may behave differently on the same providers.
- One vantage point: GitHub-hosted US runners. Connection setup was 70–170 ms for every provider, so network time doesn't explain the differences.
- Low request rate. This measures what a single client sees, not throughput under heavy load.
- Structured-output results are for one configuration: schema in `response_format` only. Providers that recommend also putting the schema in the prompt may do better when you follow that advice.
- Prices in `bench/providers.yaml` were the listed prices at the time; effective $/M is computed from them.
- A personal project, not affiliated with or funded by any provider.

## How it worked

Each run, every endpoint received the same requests in parallel. Within a provider, requests ran one at a time, so the benchmark never queued its own traffic. Every prompt started with a random request ID, so no provider could serve a cached prefix.

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
results-summary/repro/        follow-up repro evidence (run 1 output, run 2 raw responses)
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

`--since` excludes six setup runs from before the study started. The weekday/weekend table above was computed separately in Pacific time; `analyze.py` groups by UTC hour.

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
