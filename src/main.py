"""主入口：配置加载、数据分组、多进程调度、结果合并、报告生成"""

import json
import multiprocessing
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from .models import (
    MultiTurnItem, MultiTurnResult, ResponseCategory,
    RESPONSE_CATEGORY_LABELS,
)
from .utils import load_json_dataset, group_items_by_seed
from .worker import worker_loop
from .reporter import generate_html_report, compute_metrics
from .container import cleanup_stale_containers


def load_config(path: str = "config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def filter_groups(
    groups: list[tuple[MultiTurnItem, list[MultiTurnItem]]],
    seed_ids: list[str],
    risk_categories: list[str],
    max_groups: int,
) -> list[tuple[MultiTurnItem, list[MultiTurnItem]]]:
    filtered = groups

    if seed_ids:
        id_set = set(seed_ids)
        filtered = [(s, injs) for s, injs in filtered if s.id in id_set]

    if risk_categories:
        cat_set = set(risk_categories)
        new = []
        for seed, injs in filtered:
            matched_injs = [i for i in injs if i.risk_category in cat_set]
            if matched_injs:
                new.append((seed, matched_injs))
        filtered = new

    if max_groups > 0:
        filtered = filtered[:max_groups]

    return filtered


def serialize_result(r: MultiTurnResult) -> dict:
    return {
        "item": {
            "index": r.item.index, "id": r.item.id,
            "name": r.item.name, "description": r.item.description,
            "scenario": r.item.scenario, "category": r.item.category,
            "risk_category": r.item.risk_category, "subcategory": r.item.subcategory,
            "technique": r.item.technique, "data_type": r.item.data_type,
            "seed_id": r.item.seed_id, "setup": r.item.setup,
            "turns_meta": [
                {
                    "injection_in_user": t.injection_in_user,
                    "injection_in_tool": t.injection_in_tool,
                    "injection_type": t.injection_type,
                }
                for t in r.item.turns
            ],
        },
        "turns": [
            {
                "turn": tr.turn_index + 1, "user": tr.user_message,
                "agent": tr.agent_response, "success": tr.success,
                "error": tr.error, "new_logs": tr.new_logs,
                "duration": round(tr.duration, 2),
            }
            for tr in r.turn_results
        ],
        "judgment": {
            "response_category": r.response_category.value if r.response_category else None,
            "safety_verdict": r.safety_verdict.value if r.safety_verdict else None,
            "utility_verdict": r.utility_verdict.value if r.utility_verdict else None,
            "seed_utility_score": r.seed_utility_score,
            "rubric": r.rubric.to_dict() if r.rubric else None,
            "over_cautious": r.over_cautious,
            "instruction_followed": r.instruction_followed,
            "general_safety_score": r.general_safety_score,
            "triggered_risks": r.triggered_risks,
            "general_safety_reason": r.general_safety_reason,
            "reason": r.reason,
            "confidence": r.confidence,
            "num_runs": r.num_runs,
            "per_run_rubrics": [rb.to_dict() if rb else None for rb in r.per_run_rubrics],
            "per_run_categories": [c.value if c else None for c in r.per_run_categories],
        },
        "all_runs": [
            {
                "run_index": run_idx,
                "response_category": sr.response_category.value if sr.response_category else None,
                "utility_verdict": sr.utility_verdict.value if sr.utility_verdict else None,
                "seed_utility_score": sr.seed_utility_score,
                "rubric": sr.rubric.to_dict() if sr.rubric else None,
                "general_safety_score": sr.general_safety_score,
                "triggered_risks": sr.triggered_risks,
                "general_safety_reason": sr.general_safety_reason,
                "reason": sr.reason, "confidence": sr.confidence,
                "total_tool_calls": sr.total_tool_calls,
                "tool_executed": sr.tool_executed, "tool_errored": sr.tool_errored,
                "tool_error_count": sr.tool_error_count,
                "total_tokens": sr.total_tokens,
                "duration": round(sr.duration, 2),
                "judge_duration": round(sr.judge_duration, 2),
                "turns": [
                    {
                        "turn": tr.turn_index + 1, "user": tr.user_message,
                        "agent": tr.agent_response, "success": tr.success,
                        "error": tr.error, "new_logs": tr.new_logs,
                        "duration": round(tr.duration, 2),
                    }
                    for tr in sr.turn_results
                ],
            }
            for run_idx, sr in enumerate(r.all_single_runs)
        ] if r.all_single_runs else [],
        "execution": {
            "timestamp": r.timestamp,
            "success": r.overall_success,
            "error": r.overall_error,
            "total_tool_calls": r.total_tool_calls,
            "tool_executed": r.tool_executed, "tool_errored": r.tool_errored,
            "tool_error_count": r.tool_error_count,
            "total_tokens": r.total_tokens,
            "duration": round(r.duration, 2),
            "judge_duration": round(r.judge_duration, 2),
        },
    }


def save_results(results: list[MultiTurnResult], output_dir: Path, num_runs: int) -> None:
    injected = [r for r in results if r.item.data_type == "harmful"]
    effective = [r for r in injected if r.response_category not in (ResponseCategory.ERROR, None)]
    n = len(effective)
    pass1 = pass2 = pass3 = 0
    for r in effective:
        cats = [c for c in r.per_run_categories if c is not None]
        n_h = sum(1 for c in cats if c == ResponseCategory.HARMFUL)
        if n_h >= 1:
            pass1 += 1
        if n_h >= 2:
            pass2 += 1
        if cats and n_h == len(cats):
            pass3 += 1

    json_data = {
        "test_run": {
            "type": "multi-turn",
            "timestamp": datetime.now().isoformat(),
            "duration": sum(r.duration for r in results),
            "total_items": len(results),
            "harmful_count": len(injected),
            "benign_count": sum(1 for r in results if r.item.data_type == "benign"),
            "num_runs": num_runs,
            "pass_at_k": {
                "n_effective": n,
                "pass_at_1": pass1, "pass_at_2": pass2, "pass_at_3": pass3,
                "asr_at_1": round(pass1 / n * 100, 1) if n > 0 else 0,
                "asr_at_2": round(pass2 / n * 100, 1) if n > 0 else 0,
                "asr_at_3": round(pass3 / n * 100, 1) if n > 0 else 0,
            },
        },
        "results": [serialize_result(r) for r in results],
    }

    detailed_path = output_dir / "detailed.json"
    detailed_path.write_text(
        json.dumps(json_data, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(f"  detailed.json saved: {detailed_path}")

    metrics = compute_metrics(results, sum(r.duration for r in results))
    summary_path = output_dir / "summary.json"
    summary_path.write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(f"  summary.json saved: {summary_path}")


def main(config_path: str = "config.yaml") -> None:
    config = load_config(config_path)

    print("=" * 60)
    print("  Platform Security Test")
    print("=" * 60)
    print(f"  target: {config['target']['model']}")
    print(f"  judge:  {config['judge']['model']}")
    print(f"  workers: {config['run']['workers']}")
    print(f"  num_runs: {config['run']['num_runs']}")

    # 加载数据
    seeds_path = config["data"]["seeds_path"]
    injected_path = config["data"]["injected_path"]
    seeds_raw = load_json_dataset(seeds_path)
    injected_raw = load_json_dataset(injected_path)
    groups = group_items_by_seed(seeds_raw, injected_raw)
    print(f"\n  loaded: {len(seeds_raw)} seeds, {len(injected_raw)} injected -> {len(groups)} groups")

    # 过滤
    flt = config.get("filter", {})
    groups = filter_groups(
        groups,
        seed_ids=flt.get("seed_ids", []),
        risk_categories=flt.get("risk_categories", []),
        max_groups=flt.get("max_groups", 0),
    )
    total_items = sum(1 + len(injs) for _, injs in groups)
    print(f"  after filter: {len(groups)} groups, {total_items} items")

    if not groups:
        print("  no groups to test, exiting.")
        return

    # 输出目录
    model_name = config["target"]["model"].replace("/", "_")
    output_dir = Path("output") / model_name
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"  output: {output_dir}")

    # 清理残留容器
    cleanup_stale_containers()

    # 构建任务队列
    task_queue: multiprocessing.Queue = multiprocessing.Queue()
    result_queue: multiprocessing.Queue = multiprocessing.Queue()
    done_counter = multiprocessing.Value("i", 0)

    for seed, injs in groups:
        task_queue.put((seed, injs))

    # 启动 workers
    n_workers = min(config["run"]["workers"], len(groups))
    processes = []
    start_time = time.time()

    os.environ["PYTHONUNBUFFERED"] = "1"
    print(f"\n  starting {n_workers} workers for {len(groups)} groups...")

    for wid in range(n_workers):
        p = multiprocessing.Process(
            target=worker_loop,
            args=(wid, task_queue, result_queue, done_counter, config),
            daemon=True,
        )
        p.start()
        processes.append(p)

    # 边跑边收集结果，实时保存
    all_results: list[MultiTurnResult] = []
    num_runs = config["run"]["num_runs"]

    report_metadata = {
        "target_model": config["target"]["model"],
        "judge_model": config["judge"]["model"],
        "num_runs": num_runs,
        "workers": n_workers,
        "dataset_total": total_items,
        "dataset_harmful": sum(len(injs) for _, injs in groups),
        "dataset_benign": len(groups),
    }
    report_lang = config.get("report_lang", "zh")

    def _collect_and_save():
        collected = 0
        while True:
            try:
                batch = result_queue.get_nowait()
                all_results.extend(batch)
                collected += len(batch)
            except Exception:
                break
        if collected > 0:
            save_results(all_results, output_dir, num_runs)
            elapsed = time.time() - start_time
            generate_html_report(all_results, elapsed, str(output_dir / "report.html"), report_metadata, lang=report_lang)
            print(f"  [{len(all_results)}/{total_items} items saved]", flush=True)

    while any(p.is_alive() for p in processes):
        time.sleep(2)
        _collect_and_save()

    # 收集 worker 退出前最后入队的结果
    for p in processes:
        p.join(timeout=5)
    _collect_and_save()

    elapsed = time.time() - start_time
    print(f"\n  all workers done. {len(all_results)} results in {elapsed:.1f}s")

    if not all_results:
        print("  no results collected.")
        return

    # 最终保存 + 生成 HTML 报告
    save_results(all_results, output_dir, num_runs)
    html_path = output_dir / "report.html"
    generate_html_report(all_results, elapsed, str(html_path), report_metadata, lang=report_lang)

    print(f"\n  done. output: {output_dir}")


if __name__ == "__main__":
    config_file = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    main(config_file)
