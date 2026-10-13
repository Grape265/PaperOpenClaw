"""Example skill script: numeric verification for paper claims.

Contract with the host (see ``agent/skill_loader.py``):

    argv[1]  path to a JSON file containing {"arguments": {...}, ...}
    argv[2]  path to write the JSON result to

Data is exchanged through files rather than stdin/stdout pipes so the same script
works under every platform and under sandboxes that restrict piped stdio. Writing
nothing to stdout is intentional: stdout is not part of the contract.
"""

from __future__ import annotations

import json
import math
import sys
from typing import Any, Dict, List, Optional


def load_arguments(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    arguments = payload.get("arguments")
    return arguments if isinstance(arguments, dict) else {}


def as_floats(value: Any) -> List[float]:
    if value is None:
        return []
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [float(value)]
    if isinstance(value, (list, tuple)):
        out: List[float] = []
        for item in value:
            if isinstance(item, bool):
                continue
            if isinstance(item, (int, float)):
                out.append(float(item))
            elif isinstance(item, str):
                try:
                    out.append(float(item.strip()))
                except ValueError:
                    continue
        return out
    return []


def describe(values: List[float]) -> Dict[str, Any]:
    count = len(values)
    if count == 0:
        return {"count": 0}
    total = sum(values)
    mean = total / count
    variance = sum((item - mean) ** 2 for item in values) / count
    ordered = sorted(values)
    return {
        "count": count,
        "sum": round(total, 6),
        "mean": round(mean, 6),
        "min": ordered[0],
        "max": ordered[-1],
        "median": round(
            ordered[count // 2] if count % 2 else (ordered[count // 2 - 1] + ordered[count // 2]) / 2,
            6,
        ),
        "std": round(math.sqrt(variance), 6),
        "range": round(ordered[-1] - ordered[0], 6),
    }


def change_between(before: float, after: float) -> Dict[str, Any]:
    absolute = after - before
    percent: Optional[float] = None
    if before != 0:
        percent = round(absolute / abs(before) * 100.0, 4)
    return {
        "before": before,
        "after": after,
        "absolute": round(absolute, 6),
        "percent": percent,
        "ratio": round(after / before, 6) if before != 0 else None,
        "direction": "提升" if absolute > 0 else ("下降" if absolute < 0 else "持平"),
    }


def numeric_check(arguments: Dict[str, Any]) -> Dict[str, Any]:
    values = as_floats(arguments.get("values"))
    if not values:
        return {"error": "values 不能为空，且必须是数字序列"}

    labels = arguments.get("labels")
    labels = [str(item) for item in labels] if isinstance(labels, (list, tuple)) else []

    result: Dict[str, Any] = {"spread": describe(values)}

    if len(labels) == len(values) and values:
        peak = max(range(len(values)), key=lambda index: values[index])
        trough = min(range(len(values)), key=lambda index: values[index])
        result["extremes"] = {
            "max_label": labels[peak],
            "max_value": values[peak],
            "min_label": labels[trough],
            "min_value": values[trough],
        }

    compare = as_floats(arguments.get("compare"))
    if len(compare) >= 2:
        result["change"] = change_between(compare[0], compare[1])
    elif compare:
        result["warning"] = "compare 需要恰好两个数值，已忽略"

    return result


def metric_delta(arguments: Dict[str, Any]) -> Dict[str, Any]:
    before = as_floats(arguments.get("before"))
    after = as_floats(arguments.get("after"))
    if not before or not after:
        return {"error": "before 与 after 都必须是单个数字"}
    return {"change": change_between(before[0], after[0])}


def main() -> int:
    if len(sys.argv) < 3:
        return 2
    args_path, out_path = sys.argv[1], sys.argv[2]
    try:
        arguments = load_arguments(args_path)
    except Exception as exc:  # noqa: BLE001 - report through the result file
        result: Dict[str, Any] = {"error": f"无法读取参数文件：{exc}"}
    else:
        # `metric_delta` is the same script under a second tool name; dispatch on
        # which arguments the host supplied.
        if "before" in arguments or "after" in arguments:
            result = metric_delta(arguments)
        else:
            result = numeric_check(arguments)

    try:
        with open(out_path, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False)
    except Exception:  # noqa: BLE001 - nothing else we can do
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
