from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                rows.append(payload)
    return rows


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * pct))))
    return ordered[index]


def _hotspot_category(name: str) -> str:
    if name in {"sse.subscribe", "message.detached_run.produce"}:
        return "lifecycle"
    if name.startswith("chat.provider") or name.startswith("stream.fetch") or name.startswith("stream.reader"):
        return "model_stream"
    if "append_event" in name or name.startswith("run.finish") or name.startswith("run.create"):
        return "persistence"
    if name.startswith("tool.") or name.startswith("chat.tool"):
        return "tools"
    if name.startswith("stream_manager") or name.startswith("stream.parse"):
        return "frontend"
    if name.startswith("http."):
        return "http"
    return "other"


def summarize_events(paths: list[Path]) -> dict[str, Any]:
    groups: dict[str, list[float]] = defaultdict(list)
    counts: dict[str, int] = defaultdict(int)
    metric_values: dict[str, list[float]] = defaultdict(list)
    events = 0
    for path in paths:
        for event in _read_jsonl(path):
            events += 1
            name = str(event.get("name") or event.get("type") or "unknown")
            counts[name] += 1
            duration = event.get("duration_ms")
            if isinstance(duration, (int, float)):
                groups[name].append(float(duration))
            attrs = event.get("attrs")
            if isinstance(attrs, dict) and (
                name.startswith("chat.provider_") or name.startswith("trial.")
            ):
                for key, value in attrs.items():
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        metric_values[f"{name}.{key}"].append(float(value))

    hotspots = []
    for name, durations in groups.items():
        total = sum(durations)
        count = len(durations)
        hotspots.append({
            "name": name,
            "category": _hotspot_category(name),
            "count": count,
            "total_ms": round(total, 3),
            "avg_ms": round(total / count, 3) if count else 0,
            "p50_ms": round(_percentile(durations, 0.50), 3),
            "p95_ms": round(_percentile(durations, 0.95), 3),
            "max_ms": round(max(durations), 3) if durations else 0,
        })
    hotspots.sort(key=lambda item: (item["total_ms"], item["p95_ms"]), reverse=True)
    grouped_hotspots: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in hotspots:
        grouped_hotspots[str(item["category"])].append(item)
    metrics = []
    for name, values in metric_values.items():
        if not values:
            continue
        total = sum(values)
        metrics.append({
            "name": name,
            "count": len(values),
            "avg": round(total / len(values), 3),
            "p50": round(_percentile(values, 0.50), 3),
            "p95": round(_percentile(values, 0.95), 3),
            "max": round(max(values), 3),
        })
    metrics.sort(key=lambda item: (item["name"]))
    return {
        "event_count": events,
        "span_event_count": sum(len(values) for values in groups.values()),
        "counts": dict(sorted(counts.items())),
        "hotspots": hotspots,
        "hotspot_groups": dict(sorted(grouped_hotspots.items())),
        "metrics": metrics,
    }


def write_reports(summary: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "hotspots.md").write_text(_markdown_report(summary), encoding="utf-8")
    (output_dir / "hotspots.html").write_text(_html_report(summary), encoding="utf-8")


def _markdown_report(summary: dict[str, Any]) -> str:
    lines = [
        "# ChatTree Performance Hotspots",
        "",
        f"- Events: {summary.get('event_count', 0)}",
        f"- Span events: {summary.get('span_event_count', 0)}",
        "",
        "| Hotspot | Category | Count | Total ms | Avg ms | P50 ms | P95 ms | Max ms |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for item in summary.get("hotspots", []):
        lines.append(
            "| {name} | {category} | {count} | {total_ms} | {avg_ms} | {p50_ms} | {p95_ms} | {max_ms} |".format(**item)
        )
    groups = summary.get("hotspot_groups", {})
    if groups:
        lines.extend(["", "## Hotspot Groups"])
        for category, items in groups.items():
            total_ms = round(sum(float(item.get("total_ms") or 0) for item in items), 3)
            lines.append(f"- {category}: {len(items)} spans, total {total_ms} ms")
    metrics = summary.get("metrics", [])
    if metrics:
        lines.extend([
            "",
            "## Metrics",
            "",
            "| Metric | Count | Avg | P50 | P95 | Max |",
            "|---|---:|---:|---:|---:|---:|",
        ])
        for item in metrics:
            lines.append("| {name} | {count} | {avg} | {p50} | {p95} | {max} |".format(**item))
    lines.append("")
    return "\n".join(lines)


def _html_report(summary: dict[str, Any]) -> str:
    rows = []
    max_total = max([item.get("total_ms", 0) for item in summary.get("hotspots", [])] or [1])
    for item in summary.get("hotspots", []):
        width = max(2, int((float(item.get("total_ms") or 0) / max_total) * 100))
        rows.append(
            "<tr>"
            f"<td>{item['name']}</td><td>{item['category']}</td><td>{item['count']}</td><td>{item['total_ms']}</td>"
            f"<td>{item['avg_ms']}</td><td>{item['p50_ms']}</td><td>{item['p95_ms']}</td><td>{item['max_ms']}</td>"
            f"<td><div class=\"bar\" style=\"width:{width}%\"></div></td>"
            "</tr>"
        )
    metric_rows = []
    for item in summary.get("metrics", []):
        metric_rows.append(
            "<tr>"
            f"<td>{item['name']}</td><td>{item['count']}</td><td>{item['avg']}</td>"
            f"<td>{item['p50']}</td><td>{item['p95']}</td><td>{item['max']}</td>"
            "</tr>"
        )
    return """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<title>ChatTree Performance Hotspots</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 24px; color: #172026; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border-bottom: 1px solid #d7dde2; padding: 8px; text-align: left; }}
th:nth-child(n+3), td:nth-child(n+3) {{ text-align: right; }}
.bar {{ height: 10px; background: #2f7d6d; border-radius: 2px; }}
</style>
<h1>ChatTree Performance Hotspots</h1>
<p>Events: {events} &nbsp; Span events: {spans}</p>
<table>
<thead><tr><th>Hotspot</th><th>Category</th><th>Count</th><th>Total ms</th><th>Avg ms</th><th>P50 ms</th><th>P95 ms</th><th>Max ms</th><th>Heat</th></tr></thead>
<tbody>
{rows}
</tbody>
</table>
<h2>Metrics</h2>
<table>
<thead><tr><th>Metric</th><th>Count</th><th>Avg</th><th>P50</th><th>P95</th><th>Max</th></tr></thead>
<tbody>
{metric_rows}
</tbody>
</table>
</html>
""".format(
        events=summary.get("event_count", 0),
        spans=summary.get("span_event_count", 0),
        rows="\n".join(rows),
        metric_rows="\n".join(metric_rows),
    )
