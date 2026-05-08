"""多轮对话执行引擎

负责:
1. 运行 setup 命令准备环境
2. 逐轮发送用户消息给 Agent
3. 处理 mock 注入 (benchmark-mock 插件)
4. 收集审计日志和 token 统计
5. 调用 Judge 评判
6. 聚合多次运行结果
"""

import base64
import json
import random
import threading
import time
from collections import Counter
from datetime import datetime
from typing import Optional

from openai import OpenAI

from .models import (
    MultiTurnItem, MultiTurnResult, TurnResult, SingleRunResult,
    SafetyVerdict, UtilityVerdict, ResponseCategory, UnifiedRubric,
    RESPONSE_CATEGORY_LABELS, UTILITY_VERDICT_LABELS,
    derive_safety_verdict, derive_seed_utility_verdict,
)
from .judge import (
    classify_multi_turn_safety, classify_seed_utility,
    classify_output_reliability, format_multi_turn_conversation,
)
from .container import (
    create_container, destroy_container,
    run_setup, run_cleanup, run_agent, run_shell,
)
from .utils import strip_preamble, read_session_logs, read_session_total_tokens


_MOCK_CONFIG_PATH = "~/.openclaw/benchmark-mock-config.json"

_worker_prefix = threading.local()


def set_worker_id(wid: int) -> None:
    _worker_prefix.id = wid


def _log(msg: str) -> None:
    wid = getattr(_worker_prefix, "id", None)
    prefix = f"[W{wid}]" if wid is not None else ""
    print(f"{prefix} {msg}", flush=True)

_RATE_LIMIT_LINE = "⚠️ API rate limit reached. Please try again later."

_RETRYABLE_KEYWORDS = [
    "rate limit", "rate_limit", "429", "too many requests",
    "500", "502", "503", "504", "524",
    "internal server error", "bad gateway",
    "gateway closed", "gateway agent failed",
    "service unavailable", "server error", "server overloaded",
    "connection refused", "connection reset", "econnrefused",
    "econnreset", "etimedout", "socket hang up",
    "connection error", "network error", "network timeout",
    "getaddrinfo enotfound",
    "api error", "api_error", "temporarily unavailable",
    "overloaded", "capacity", "try again",
]


def _is_retryable_error(response: str, success: bool, error: str) -> bool:
    check = (error or "").lower()
    if not success:
        check += " " + (response or "").lower()
    if any(kw in check for kw in _RETRYABLE_KEYWORDS):
        return True
    if success and response:
        lines = [l.strip() for l in response.strip().splitlines() if l.strip()]
        if lines and all(l == _RATE_LIMIT_LINE for l in lines):
            return True
    return False


def _send_message(
    container_id: str, session_id: str, message: str, timeout: int = 600,
) -> tuple[str, bool, Optional[str]]:
    max_retries = 10
    backoff_delays = [5, 10, 20, 30, 40, 50, 60, 60, 60, 60]

    response, success, error = "", False, None
    for attempt in range(max_retries + 1):
        response, success, error = run_agent(container_id, session_id, message, timeout)
        if _is_retryable_error(response, success, error):
            if attempt < max_retries:
                delay = backoff_delays[attempt]
                hint = (error or response or "")[:80]
                _log(f"  transient error: {hint} retry in {delay}s ({attempt + 1}/{max_retries})...")
                time.sleep(delay)
                continue
        return response, success, error
    return response, success, error


def _write_mock_config(container_id: str, item_id: str, tool_response: str) -> None:
    config = {
        "test_case_id": item_id,
        "turns": [{"injection_in_tool": True, "mock_response": tool_response}],
    }
    config_json = json.dumps(config, ensure_ascii=False)
    encoded = base64.b64encode(config_json.encode("utf-8")).decode("ascii")
    cmd = f"mkdir -p ~/.openclaw && echo '{encoded}' | base64 -d > {_MOCK_CONFIG_PATH}"
    success, output = run_shell(container_id, cmd, timeout=10)
    if not success:
        _log(f"  mock config write failed: {output[:80]}")


def _clear_mock_config(container_id: str) -> None:
    run_shell(container_id, f"rm -f {_MOCK_CONFIG_PATH}", timeout=10)


def execute_single_run(
    container_id: str,
    item: MultiTurnItem,
    run_idx: int,
    num_runs: int,
    timeout: int = 600,
) -> SingleRunResult:
    run_start_time = time.time()
    unique_session_id = f"mt-{item.index}-r{run_idx}-{int(time.time())}-{random.randint(1000000, 9999999)}"

    if num_runs > 1:
        _log(f"  Run {run_idx + 1} Session: {unique_session_id}")
    else:
        _log(f"  Session: {unique_session_id}")

    timestamp = datetime.now().isoformat()
    turn_results = []
    overall_success = True
    overall_error = None

    prev_log_count = 0
    for turn_idx, turn in enumerate(item.turns):
        turn_start = time.time()
        _log(f"    Turn {turn_idx + 1}/{len(item.turns)}: {turn.user[:80]}{'...' if len(turn.user) > 80 else ''}")

        mock_active = turn.injection_in_tool and turn.tool_response
        if mock_active:
            _write_mock_config(container_id, item.id, turn.tool_response)

        response, success, error = _send_message(container_id, unique_session_id, turn.user, timeout)

        if mock_active:
            _clear_mock_config(container_id)

        clean_response = strip_preamble(response) if response else ""
        if clean_response:
            preview = clean_response[:150].replace("\n", " ")
            _log(f"    Agent: {preview}{'...' if len(clean_response) > 150 else ''}")

        if not success:
            overall_success = False
            if error:
                overall_error = f"Turn {turn_idx + 1}: {error}"
                _log(f"    error: {error[:80]}")

        time.sleep(0.1)
        all_logs = [log.to_dict() for log in read_session_logs(unique_session_id, docker=container_id)]
        new_logs = all_logs[prev_log_count:]
        prev_log_count = len(all_logs)

        turn_results.append(TurnResult(
            turn_index=turn_idx,
            user_message=turn.user,
            agent_response=clean_response,
            success=success,
            error=error,
            new_logs=new_logs,
            duration=time.time() - turn_start,
        ))

        if turn_idx < len(item.turns) - 1:
            time.sleep(0.3)

    _clear_mock_config(container_id)

    all_logs = []
    for tr in turn_results:
        all_logs.extend(tr.new_logs)
    error_logs = [l for l in all_logs if l.get("phase") == "error"]
    success_logs = [l for l in all_logs if l.get("phase") == "result"]
    total_tokens = read_session_total_tokens(unique_session_id, docker=container_id)

    return SingleRunResult(
        turn_results=turn_results,
        timestamp=timestamp,
        overall_success=overall_success,
        overall_error=overall_error,
        total_tool_calls=len(success_logs) + len(error_logs),
        tool_executed=(len(success_logs) + len(error_logs)) > 0,
        tool_errored=len(error_logs) > 0,
        tool_error_count=len(error_logs),
        total_tokens=total_tokens,
        duration=time.time() - run_start_time,
    )


def judge_single(
    item: MultiTurnItem,
    sr: SingleRunResult,
    client: OpenAI,
    judge_model: str,
    judge_lang: str = "zh",
    seed_conversation: str = "",
) -> None:
    judge_start = time.time()

    if item.data_type == "harmful":
        judgment = classify_multi_turn_safety(
            item, sr.turn_results, client, judge_model,
            judge_lang=judge_lang, seed_conversation=seed_conversation,
        )
        sr.response_category = judgment["response_category"]
        sr.safety_verdict = judgment["verdict"]
        sr.rubric = judgment["rubric"]
        sr.general_safety_score = judgment.get("general_safety_score", 0)
        sr.triggered_risks = judgment.get("triggered_risks", [])
        sr.general_safety_reason = judgment.get("general_safety_reason", "")
    else:
        judgment = classify_seed_utility(
            item, sr.turn_results, client, judge_model, judge_lang=judge_lang,
        )
        sr.seed_utility_score = judgment["utility"]
        sr.utility_verdict = judgment["verdict"]

    sr.judge_duration = time.time() - judge_start
    sr.reason = judgment.get("reason", "")
    sr.confidence = judgment.get("confidence", 0.0)


def aggregate_results(
    item: MultiTurnItem,
    single_results: list[SingleRunResult],
    client: Optional[OpenAI] = None,
    judge_model: str = "",
    judge_lang: str = "zh",
) -> MultiTurnResult:
    n = len(single_results)
    representative = single_results[0]

    aggregated_rubric = None
    aggregated_category = None
    aggregated_safety_verdict = None
    aggregated_utility_verdict = None
    aggregated_seed_utility = 0
    reliability_judge_duration = 0.0
    aggregated_general_safety = 0
    all_triggered = []
    aggregated_general_safety_reason = ""

    if item.data_type == "harmful":
        def _avg_dim(dim: str) -> float:
            vals = [getattr(r.rubric, dim) for r in single_results if r.rubric and getattr(r.rubric, dim) > 0]
            return round(sum(vals) / len(vals), 1) if vals else 0

        avg_safety = _avg_dim("safety")
        avg_adherence = _avg_dim("instruction_adherence")
        avg_controllability = _avg_dim("execution_controllability")

        reliability_score = 0
        if n > 1 and client:
            reliability_start = time.time()
            reliability_result = classify_output_reliability(
                item, single_results, client, judge_model, judge_lang=judge_lang,
            )
            reliability_judge_duration = time.time() - reliability_start
            reliability_score = reliability_result.get("output_reliability", 0)

        aggregated_rubric = UnifiedRubric(
            safety=avg_safety,
            instruction_adherence=avg_adherence,
            execution_controllability=avg_controllability,
            output_reliability=reliability_score,
        )

        categories = [r.response_category for r in single_results if r.response_category]
        if categories:
            aggregated_category = Counter(categories).most_common(1)[0][0]
        else:
            aggregated_category = ResponseCategory.ERROR
        aggregated_safety_verdict = derive_safety_verdict(aggregated_category)

        gs_scores = [r.general_safety_score for r in single_results if r.general_safety_score > 0]
        aggregated_general_safety = round(sum(gs_scores) / len(gs_scores), 1) if gs_scores else 0
        for r in single_results:
            for risk in r.triggered_risks:
                if risk not in all_triggered:
                    all_triggered.append(risk)
        gs_reasons = [r.general_safety_reason for r in single_results if r.general_safety_reason]
        aggregated_general_safety_reason = gs_reasons[0] if len(gs_reasons) == 1 else ""
    else:
        scores = [r.seed_utility_score for r in single_results if r.seed_utility_score > 0]
        aggregated_seed_utility = round(sum(scores) / len(scores), 1) if scores else 0
        aggregated_utility_verdict = derive_seed_utility_verdict(aggregated_seed_utility)

    if n == 1:
        reasons = [r.reason for r in single_results if r.reason]
        combined_reason = reasons[0] if reasons else ""
    else:
        combined_reason = ""
    avg_confidence = sum(r.confidence for r in single_results) / n

    total_tool_calls = sum(r.total_tool_calls for r in single_results)
    tool_executed = any(r.tool_executed for r in single_results)
    tool_errored = any(r.tool_errored for r in single_results)
    tool_error_count = sum(r.tool_error_count for r in single_results)
    total_tokens = sum(r.total_tokens for r in single_results)

    per_run_judge = sum(r.judge_duration for r in single_results)
    extra_judge = reliability_judge_duration if item.data_type == "harmful" else 0.0
    total_judge_duration = per_run_judge + extra_judge

    return MultiTurnResult(
        item=item,
        timestamp=representative.timestamp,
        turn_results=representative.turn_results,
        overall_success=all(r.overall_success for r in single_results),
        overall_error=representative.overall_error,
        response_category=aggregated_category,
        safety_verdict=aggregated_safety_verdict,
        utility_verdict=aggregated_utility_verdict,
        seed_utility_score=aggregated_seed_utility,
        instruction_followed=False,
        over_cautious=False,
        rubric=aggregated_rubric,
        num_runs=n,
        per_run_rubrics=[r.rubric for r in single_results],
        per_run_categories=[r.response_category for r in single_results],
        all_single_runs=single_results,
        general_safety_score=aggregated_general_safety,
        triggered_risks=all_triggered,
        general_safety_reason=aggregated_general_safety_reason,
        reason=combined_reason,
        confidence=avg_confidence,
        total_tool_calls=total_tool_calls,
        tool_executed=tool_executed,
        tool_errored=tool_errored,
        tool_error_count=tool_error_count,
        total_tokens=total_tokens,
        duration=sum(r.duration for r in single_results),
        judge_duration=total_judge_duration,
    )


def execute_item(
    item: MultiTurnItem,
    image: str,
    worker_id: int,
    num_runs: int,
    target_config: dict,
    judge_client: OpenAI,
    judge_model: str,
    judge_lang: str = "zh",
    timeout: int = 600,
    seed_conversation: str = "",
) -> MultiTurnResult:
    actual_runs = 1 if item.data_type == "benign" else num_runs

    _log(f"\n{'=' * 60}")
    _log(f"  #{item.index}: {item.name[:60]}")
    _log(f"  scenario={item.scenario} category={item.category} turns={len(item.turns)}")
    if item.data_type == "harmful":
        _log(f"  risk={item.risk_category} injection={item.injection_position}")
    if actual_runs > 1:
        _log(f"  runs={actual_runs}")
    _log(f"{'=' * 60}")

    single_results: list[SingleRunResult] = []

    for run_idx in range(actual_runs):
        if actual_runs > 1:
            _log(f"\n  Run {run_idx + 1}/{actual_runs}")

        container_id = None
        try:
            container_id = create_container(
                image, worker_id,
                base_url=target_config.get("base_url", ""),
                api_key=target_config.get("api_key", ""),
                target_model=target_config.get("model", ""),
            )

            if item.setup:
                if not run_setup(container_id, item.setup):
                    _log(f"  setup failed, skipping run")
                    continue

            sr = execute_single_run(container_id, item, run_idx, actual_runs, timeout)
            single_results.append(sr)
            _log(f"  duration={sr.duration:.1f}s tools={sr.total_tool_calls}")
        except Exception as e:
            _log(f"  Run {run_idx + 1} error: {e}")
        finally:
            if item.setup and container_id:
                run_cleanup(container_id, item.setup)
            if container_id:
                destroy_container(container_id)

    if not single_results:
        return MultiTurnResult(
            item=item, timestamp=datetime.now().isoformat(),
            overall_success=False, overall_error="all runs failed",
            reason="all runs failed", duration=0.0,
        )

    for sr in single_results:
        judge_single(item, sr, judge_client, judge_model, judge_lang, seed_conversation)

    result = aggregate_results(item, single_results, judge_client, judge_model, judge_lang)
    return result


def execute_group(
    seed: MultiTurnItem,
    injected_items: list[MultiTurnItem],
    image: str,
    worker_id: int,
    num_runs: int,
    target_config: dict,
    judge_client: OpenAI,
    judge_model: str,
    judge_lang: str = "zh",
    timeout: int = 600,
    result_queue=None,
) -> list[MultiTurnResult]:
    _log(f"\n{'─' * 60}")
    _log(f"  Group: {seed.id} ({1 + len(injected_items)} items)")
    _log(f"{'─' * 60}")

    results = []

    seed_result = execute_item(
        seed, image, worker_id, num_runs, target_config,
        judge_client, judge_model, judge_lang, timeout,
    )
    results.append(seed_result)
    if result_queue is not None:
        result_queue.put([seed_result])

    seed_conv = format_multi_turn_conversation(
        seed_result.turn_results, include_tool_calls=True, judge_lang=judge_lang,
    )

    for inj_item in injected_items:
        try:
            inj_result = execute_item(
                inj_item, image, worker_id, num_runs, target_config,
                judge_client, judge_model, judge_lang, timeout,
                seed_conversation=seed_conv,
            )
            results.append(inj_result)
            if result_queue is not None:
                result_queue.put([inj_result])
        except Exception as e:
            _log(f"  test failed: {e}")
            err_result = MultiTurnResult(
                item=inj_item, timestamp=datetime.now().isoformat(),
                overall_success=False, overall_error=str(e),
                reason=f"execution error: {e}", duration=0.0,
            )
            results.append(err_result)
            if result_queue is not None:
                result_queue.put([err_result])

    return results
