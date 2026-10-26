# inference-bench

A week-long benchmark of serverless inference providers serving the same open-weight model (gpt-oss-120b, reasoning effort medium). Every 30 minutes, a GitHub Actions job sends an identical request set to each provider in parallel and logs latency, throughput, errors, correctness and cost.

Providers: Together, Fireworks, Baseten, DeepInfra, Cerebras, and OpenRouter (pinned to Together, fallbacks off).

## What each run sends (per provider)

| Test | Shape | Count | Graded on |
|---|---|---|---|
| short | ~60-token prompt → ~200-word answer | 5 | non-empty answer |
| medium | ~2K-token log dump → ~400-word summary | 3 | non-empty answer |
| structured | ticket text → JSON schema | 3 | schema-valid **and** all 4 fields correct |
| tool | natural-language command, 2 tools offered | 3 | right tool **and** exact args |
| long | ~32K-token log dump with a hidden "needle" | 2, every 6th run | needle code returned |

Every prompt starts with a random request ID, so no provider can serve a cached prefix.

## What gets logged (one JSONL row per request)

- **Latency**:
  - `ttft_s`: first token of any kind, including reasoning
  - `ttfa_s`: first visible answer token
  - `tpot_ms`, `output_tps`, `e2e_s`
- **Tokens and cost**: input/output/reasoning tokens from the provider's `usage`, and `cost_usd` from the prices in `providers.yaml`
- **Errors**: `rate_limited`, `billing`, `auth`, `server_error`, `client_error`, `timeout`, `network`, `stream_error`, `empty_output`; plus `truncated` (hit max_tokens)
- **Correctness**: `check_pass`, plus per-test detail (`schema_valid`, `fields_correct`, `tool_name_ok`, `tool_args_ok`)
- **Network**: `tcp_ms` / `tls_ms` from a bare handshake per provider per run, to separate network time from server time

## Setup

1. **Create a private repo** and push this folder to `main`.

2. **Create the results branch** (one time). Results are committed there so `main` stays clean:
   ```bash
   git checkout --orphan data
   git rm -rf . && mkdir results && touch results/.keep
   git add results && git commit -m "init data branch" && git push origin data
   git checkout main
   ```

3. **Add six repository secrets** (Settings → Secrets and variables → Actions):
   - `TOGETHER_API_KEY`
   - `FIREWORKS_API_KEY`
   - `BASETEN_API_KEY`
   - `DEEPINFRA_API_KEY`
   - `CEREBRAS_API_KEY`
   - `OPENROUTER_API_KEY`

4. **Verify** `bench/providers.yaml`: model IDs, prices, and the OpenRouter upstream slug against each provider's docs.

5. **Dry run locally first** (see below), then trigger the workflow manually from the Actions tab, then set up the cron-job.org trigger.

## Local dry run

```bash
pip install -r requirements.txt
export TOGETHER_API_KEY=...   # etc.
python bench/run.py --tests short,tool        # quick, cheap
python bench/run.py --long                    # full suite incl. 32K context
python bench/analyze.py --results results --out analysis
```

What to check in the dry-run output:

- **Every provider shows N/N ok.** A `client_error` with a 400 usually means a parameter that provider rejects. Most likely `reasoning_effort`, `response_format`, or `stream_options`; read `error_detail`.
- **`reason tok` is sensible.** This drives cost. Over ~1,000 means medium effort is running long.
- **Long-context `input_tokens` is near 32K.** If not, adjust `TOKENS_PER_LOG_LINE` in `prompts.py`.
- **Runtime of the slowest provider.** That's the job's runtime.

## Analysis

```bash
git fetch origin data && git worktree add data origin/data
python bench/analyze.py --results data/results --out analysis
```

This writes `analysis/summary.md` plus one CSV per table:
- latency/throughput percentiles
- correctness rates
- errors by type
- cost and effective $/M
- p95 TTFT by hour of day
- connect time vs. TTFT

## Budget

- **API spend:** about 7.9M input + 2.8M output tokens per provider per week, which is $2–5 per provider at listed prices.
- **Actions minutes:** each run takes as long as the slowest provider. 336 runs × ~3–5 min is ~1,000–1,700 min, which is near the 2,000 free minutes for a private repo. If dry runs show >5 min per run, make the repo public (unlimited minutes) or change the cron to hourly.
