#!/usr/bin/env python3
"""Aggregate all JSONL results into summary tables (markdown + CSV).

Usage:
  python bench/analyze.py --results data/results --out analysis
"""
import argparse
from pathlib import Path

import duckdb


def md_table(df) -> str:
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for _, row in df.iterrows():
        cells = []
        for v in row:
            if v is None or (isinstance(v, float) and v != v):
                cells.append("–")
            elif isinstance(v, float):
                cells.append(f"{v:.3f}" if abs(v) < 10 else f"{v:.1f}")
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


QUERIES = {
    "Latency and throughput by provider and test": """
        SELECT provider, test, count(*) AS n,
          round(100.0 * avg(CASE WHEN error IS NULL THEN 0 ELSE 1 END), 2) AS err_pct,
          quantile_cont(ttft_s, 0.50) AS ttft_p50,
          quantile_cont(ttft_s, 0.95) AS ttft_p95,
          quantile_cont(ttft_s, 0.99) AS ttft_p99,
          quantile_cont(ttfa_s, 0.50) AS ttfa_p50,
          quantile_cont(output_tps, 0.50) AS tps_p50,
          quantile_cont(output_tps, 0.05) AS tps_p05,
          quantile_cont(e2e_s, 0.95) AS e2e_p95
        FROM r GROUP BY ALL ORDER BY test, ttft_p50
    """,
    "Correctness (structured output, tool calling, long-context needle)": """
        SELECT provider, test, count(*) FILTER (WHERE error IS NULL) AS n_ok,
          round(100.0 * avg(CAST(check_pass AS INT)) FILTER (WHERE error IS NULL), 1) AS pass_pct,
          round(100.0 * avg(CAST(truncated AS INT)), 1) AS truncated_pct
        FROM r WHERE test IN ('structured', 'tool', 'long')
        GROUP BY ALL ORDER BY test, pass_pct DESC
    """,
    "Errors by type": """
        SELECT provider, error, count(*) AS n
        FROM r WHERE error IS NOT NULL
        GROUP BY ALL ORDER BY provider, n DESC
    """,
    "Cost and tokens": """
        SELECT provider, count(*) AS requests,
          sum(input_tokens) AS input_tok, sum(output_tokens) AS output_tok,
          median(reasoning_tokens) AS reasoning_tok_p50,
          round(sum(cost_usd), 2) AS cost_usd,
          round(1e6 * sum(cost_usd) / nullif(sum(input_tokens + output_tokens), 0), 3)
            AS effective_usd_per_m
        FROM r GROUP BY ALL ORDER BY cost_usd DESC
    """,
    "Short-test TTFT p95 by hour of day (UTC)": """
        PIVOT (
          SELECT provider, hour(CAST(ts_utc AS TIMESTAMP)) AS hr, ttft_s
          FROM r WHERE test = 'short' AND error IS NULL
        ) ON provider USING round(quantile_cont(ttft_s, 0.95), 3) GROUP BY hr ORDER BY hr
    """,
    "Network vs. server: median TCP+TLS connect vs. TTFT": """
        SELECT provider,
          median(tcp_ms + tls_ms) AS connect_ms_p50,
          1000 * median(ttft_s) AS ttft_ms_p50
        FROM r WHERE error IS NULL GROUP BY ALL ORDER BY ttft_ms_p50
    """,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="data/results")
    ap.add_argument("--out", default="analysis")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.execute(f"""
        CREATE VIEW r AS SELECT * FROM read_json_auto(
          '{args.results}/**/*.jsonl', format='newline_delimited',
          union_by_name=true, ignore_errors=true)
    """)
    n, runs = con.execute("SELECT count(*), count(DISTINCT run_id) FROM r").fetchone()
    lo, hi = con.execute("SELECT min(ts_utc)::VARCHAR, max(ts_utc)::VARCHAR FROM r").fetchone()

    report = [f"# Inference provider benchmark\n",
              f"{n} requests across {runs} runs, {lo} → {hi}\n"]
    for i, (title, sql) in enumerate(QUERIES.items()):
        try:
            df = con.execute(sql).df()
        except duckdb.Error as e:
            report.append(f"## {title}\n\n_(query failed: {e})_\n")
            continue
        df.to_csv(out / f"{i + 1:02d}_{title.split(' ')[0].lower()}.csv", index=False)
        report.append(f"## {title}\n\n{md_table(df)}\n")

    (out / "summary.md").write_text("\n".join(report))
    print("\n".join(report))


if __name__ == "__main__":
    main()
