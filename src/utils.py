"""
工具函数模块

包含数据加载、JSON 解析、session 日志读取等通用函数。
合并自 Agent_Evaluation 的 utils.py 和 tool_audit_logger.py。
"""

import json
import os
import re
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

from .models import (
    MultiTurnItem, TurnData, TurnResult, MultiTurnResult, SingleRunResult,
    UnifiedRubric, ResponseCategory, SafetyVerdict, UtilityVerdict,
    derive_safety_verdict,
)


def parse_llm_json(text: str) -> dict:
    json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1).strip())
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    brace_match = re.search(r'\{.*\}', text, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group())
        except json.JSONDecodeError:
            pass
    return {}


def strip_preamble(response: str) -> str:
    lines = response.split("\n")
    start_index = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(">") and "openclaw" in stripped:
            start_index = i + 1
            continue
        if stripped == "" and i == start_index:
            start_index = i + 1
            continue
        if stripped.startswith("[Security Audit]"):
            start_index = i + 1
            continue
        if "Waiting for agent reply" in stripped:
            start_index = i + 1
            continue
        if stripped == "⚠️ API rate limit reached. Please try again later.":
            start_index = i + 1
            continue
        break
    return "\n".join(lines[start_index:]).strip()


def _infer_category(turns: list[TurnData]) -> str:
    has_user_inj = any(t.injection_in_user for t in turns)
    has_tool_inj = any(t.injection_in_tool for t in turns)
    inj_turns = sum(1 for t in turns if t.injection_in_user or t.injection_in_tool)
    if has_user_inj and has_tool_inj:
        return "multi-turn"
    if inj_turns > 1:
        return "multi-turn"
    if has_tool_inj:
        return "indirect-injection"
    if has_user_inj:
        return "direct-injection"
    return "normal"


def load_json_dataset(json_path: str, data_type: str = "auto") -> list[MultiTurnItem]:
    path = Path(json_path)
    if not path.exists():
        raise FileNotFoundError(f"JSON 文件不存在: {json_path}")

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        if isinstance(data, dict) and "test_cases" in data:
            data = data["test_cases"]
        else:
            data = [data]

    if data_type == "auto":
        has_injection = any(
            any(t.get("injection_in_user") or t.get("injection_in_tool") for t in item.get("turns", []))
            for item in data
        )
        data_type = "harmful" if has_injection else "benign"
    if data_type == "injected":
        data_type = "harmful"
    elif data_type == "seeds":
        data_type = "benign"

    items = []
    for i, item in enumerate(data):
        meta = item.get("_meta", {})
        turns = []
        for t in item.get("turns", []):
            turns.append(TurnData(
                user=t.get("user", ""),
                tool_response=t.get("tool_response"),
                tool_name=t.get("tool_name"),
                injection_in_user=t.get("injection_in_user", False),
                injection_in_tool=t.get("injection_in_tool", False),
                injection_type=t.get("injection_type"),
            ))

        category = item.get("category") or _infer_category(turns)
        scenario = item.get("scenario") or meta.get("scenario", "unknown")
        risk_category = item.get("risk_category") or meta.get("risk_category", "")
        subcategory = item.get("subcategory") or meta.get("subcategory", "")
        technique = item.get("technique") or meta.get("technique", "")

        items.append(MultiTurnItem(
            index=i + 1,
            id=item.get("id", f"{data_type}-{i + 1:04d}"),
            name=item.get("name", f"{data_type}_{scenario}_{i + 1}"),
            description=item.get("description", ""),
            category=category,
            scenario=scenario,
            turns=turns,
            setup=item.get("setup", []),
            risk_category=risk_category,
            subcategory=subcategory,
            technique=technique,
            data_type=data_type,
            seed_id=item.get("seed_id", "") or item.get("group_id", ""),
        ))

    return items


def group_items_by_seed(
    seeds: list[MultiTurnItem],
    injected: list[MultiTurnItem],
) -> list[tuple[MultiTurnItem, list[MultiTurnItem]]]:
    inj_by_seed: dict[str, list[MultiTurnItem]] = {}
    for item in injected:
        key = item.seed_id
        if key:
            inj_by_seed.setdefault(key, []).append(item)

    groups = []
    for seed in sorted(seeds, key=lambda s: s.index):
        matched = inj_by_seed.get(seed.id, [])
        groups.append((seed, matched))
    return groups


def filter_groups(
    groups: list[tuple[MultiTurnItem, list[MultiTurnItem]]],
    seed_ids: list[str] | None = None,
    risk_categories: list[str] | None = None,
    max_groups: int = 0,
) -> list[tuple[MultiTurnItem, list[MultiTurnItem]]]:
    if seed_ids:
        allowed_ids = set(seed_ids)
        groups = [(s, injs) for s, injs in groups if s.id in allowed_ids]
    if risk_categories:
        allowed = set(risk_categories)
        groups = [(s, injs) for s, injs in groups if s.risk_category in allowed]
    if max_groups > 0:
        groups = groups[:max_groups]
    return groups


# ══════════════════════════════════════════════
# JSON → 对象反序列化
# ══════════════════════════════════════════════

def _parse_rubric(d: dict | None) -> UnifiedRubric | None:
    if not d:
        return None
    return UnifiedRubric(
        safety=d.get("safety", 0),
        instruction_adherence=d.get("instruction_adherence", 0),
        execution_controllability=d.get("execution_controllability", 0),
        output_reliability=d.get("output_reliability", 0),
    )


def _parse_enum(val: str | None, enum_cls):
    if val is None:
        return None
    if enum_cls is ResponseCategory and val in ("complete", "partial"):
        return ResponseCategory.HARMFUL
    try:
        return enum_cls(val)
    except (ValueError, KeyError):
        return None


def _parse_turn_results(turns_data: list[dict]) -> list[TurnResult]:
    return [
        TurnResult(
            turn_index=t.get("turn", 1) - 1,
            user_message=t.get("user", ""),
            agent_response=t.get("agent", ""),
            success=t.get("success", True),
            error=t.get("error"),
            new_logs=t.get("new_logs", []),
            duration=t.get("duration", 0.0),
        )
        for t in turns_data
    ]


def load_results_from_json(json_path: str) -> tuple[list[MultiTurnResult], float, dict]:
    path = Path(json_path)
    if not path.exists():
        raise FileNotFoundError(f"JSON 文件不存在: {json_path}")

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    test_run = data.get("test_run", {})
    duration = test_run.get("duration", 0.0)
    metadata = test_run.get("metadata", {})
    results: list[MultiTurnResult] = []

    for entry in data.get("results", []):
        item_d = entry.get("item", {})
        turns_meta = item_d.get("turns_meta", [])
        turns_data = entry.get("turns", [])

        turn_data_list: list[TurnData] = []
        for idx, tm in enumerate(turns_meta):
            user_msg = turns_data[idx].get("user", "") if idx < len(turns_data) else ""
            turn_data_list.append(TurnData(
                user=user_msg,
                injection_in_user=tm.get("injection_in_user", False),
                injection_in_tool=tm.get("injection_in_tool", False),
                injection_type=tm.get("injection_type"),
            ))

        item = MultiTurnItem(
            index=item_d.get("index", 0),
            id=item_d.get("id", ""),
            name=item_d.get("name", ""),
            description=item_d.get("description", ""),
            category=item_d.get("category", ""),
            scenario=item_d.get("scenario", ""),
            turns=turn_data_list,
            setup=item_d.get("setup", []),
            risk_category=item_d.get("risk_category", ""),
            subcategory=item_d.get("subcategory", ""),
            technique=item_d.get("technique", ""),
            data_type=item_d.get("data_type", ""),
            seed_id=item_d.get("seed_id", "") or item_d.get("group_id", ""),
        )

        turn_results = _parse_turn_results(turns_data)
        jdg = entry.get("judgment", {})
        response_category = _parse_enum(jdg.get("response_category"), ResponseCategory)
        safety_verdict = _parse_enum(jdg.get("safety_verdict"), SafetyVerdict)
        utility_verdict = _parse_enum(jdg.get("utility_verdict"), UtilityVerdict)
        rubric = _parse_rubric(jdg.get("rubric"))
        per_run_rubrics = [_parse_rubric(rb) for rb in jdg.get("per_run_rubrics", [])]
        per_run_categories = [_parse_enum(c, ResponseCategory) for c in jdg.get("per_run_categories", [])]

        all_single_runs: list[SingleRunResult] = []
        for run_d in entry.get("all_runs", []):
            sr_category = _parse_enum(run_d.get("response_category"), ResponseCategory)
            sr_safety = derive_safety_verdict(sr_category) if sr_category else None
            sr_utility = _parse_enum(run_d.get("utility_verdict"), UtilityVerdict)
            sr_turns = _parse_turn_results(run_d.get("turns", []))
            all_single_runs.append(SingleRunResult(
                turn_results=sr_turns,
                overall_success=all(t.success for t in sr_turns),
                response_category=sr_category,
                safety_verdict=sr_safety,
                utility_verdict=sr_utility,
                seed_utility_score=run_d.get("seed_utility_score", 0),
                rubric=_parse_rubric(run_d.get("rubric")),
                general_safety_score=run_d.get("general_safety_score", 0),
                triggered_risks=run_d.get("triggered_risks", []),
                general_safety_reason=run_d.get("general_safety_reason", ""),
                reason=run_d.get("reason", ""),
                confidence=run_d.get("confidence", 0.0),
                total_tool_calls=run_d.get("total_tool_calls", 0),
                tool_executed=run_d.get("tool_executed", False),
                tool_errored=run_d.get("tool_errored", False),
                tool_error_count=run_d.get("tool_error_count", 0),
                total_tokens=run_d.get("total_tokens", 0),
                duration=run_d.get("duration", 0.0),
                judge_duration=run_d.get("judge_duration", 0.0),
            ))

        exe = entry.get("execution", {})
        results.append(MultiTurnResult(
            item=item,
            timestamp=exe.get("timestamp", ""),
            turn_results=turn_results,
            overall_success=exe.get("success", True),
            overall_error=exe.get("error"),
            response_category=response_category,
            safety_verdict=safety_verdict,
            utility_verdict=utility_verdict,
            seed_utility_score=jdg.get("seed_utility_score", 0),
            instruction_followed=jdg.get("instruction_followed", False),
            over_cautious=jdg.get("over_cautious", False),
            rubric=rubric,
            num_runs=jdg.get("num_runs", 1),
            per_run_rubrics=per_run_rubrics,
            per_run_categories=per_run_categories,
            all_single_runs=all_single_runs,
            general_safety_score=jdg.get("general_safety_score", 0),
            triggered_risks=jdg.get("triggered_risks", []),
            general_safety_reason=jdg.get("general_safety_reason", ""),
            reason=jdg.get("reason", ""),
            confidence=jdg.get("confidence", 0.0),
            total_tool_calls=exe.get("total_tool_calls", 0),
            tool_executed=exe.get("tool_executed", False),
            tool_errored=exe.get("tool_errored", False),
            tool_error_count=exe.get("tool_error_count", 0),
            total_tokens=exe.get("total_tokens", 0),
            duration=exe.get("duration", 0.0),
            judge_duration=exe.get("judge_duration", 0.0),
        ))

    return results, duration, metadata


# ══════════════════════════════════════════════
# Session Tool Call Logger (from tool_audit_logger.py)
# ══════════════════════════════════════════════

@dataclass
class ToolAuditLogEntry:
    timestamp: str
    tool_call_id: str
    tool_name: str
    phase: str
    session_id: str = ""
    args: Optional[dict[str, Any]] = None
    result: Optional[Any] = None
    error: Optional[str] = None
    duration_ms: Optional[float] = None
    run_id: str = ""
    session_key: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        return {k: v for k, v in data.items() if v is not None}


def _read_file_lines(file_path: str, docker: Optional[str] = None, docker_home: str = "/home/node") -> list[str]:
    if docker:
        try:
            result = subprocess.run(
                ["docker", "exec", "-u", "root", "-e", f"HOME={docker_home}",
                 docker, "sh", "-c", f"cat {file_path}"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                return []
            return [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
        except Exception:
            return []
    else:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return [line.strip() for line in f if line.strip()]
        except Exception:
            return []


def _file_exists(file_path: str, docker: Optional[str] = None, docker_home: str = "/home/node") -> bool:
    if docker:
        try:
            result = subprocess.run(
                ["docker", "exec", "-u", "root", "-e", f"HOME={docker_home}",
                 docker, "sh", "-c", f"test -f {file_path}"],
                capture_output=True, text=True, timeout=10,
            )
            return result.returncode == 0
        except Exception:
            return False
    else:
        return os.path.isfile(file_path)


def _resolve_session_file(session_id: str, docker: Optional[str] = None, docker_home: str = "/home/node") -> str:
    if docker:
        sessions_dir = "~/.openclaw/agents/main/sessions"
    else:
        home = os.environ.get("HOME") or os.path.expanduser("~")
        sessions_dir = str(Path(home) / ".openclaw" / "agents" / "main" / "sessions")

    direct_path = f"{sessions_dir}/{session_id}.jsonl"
    if _file_exists(direct_path, docker, docker_home):
        return direct_path

    sessions_json_path = f"{sessions_dir}/sessions.json"
    lines = _read_file_lines(sessions_json_path, docker, docker_home)
    if lines:
        try:
            data = json.loads("\n".join(lines))
            key = f"agent:main:explicit:{session_id}"
            if key in data:
                uuid = data[key].get("sessionId", "")
                if uuid:
                    return f"{sessions_dir}/{uuid}.jsonl"
        except (json.JSONDecodeError, KeyError):
            pass
    return direct_path


def read_session_logs(session_id: str, docker: Optional[str] = None, docker_home: str = "/home/node") -> list[ToolAuditLogEntry]:
    file_path = _resolve_session_file(session_id, docker, docker_home)
    lines = _read_file_lines(file_path, docker, docker_home=docker_home)
    if not lines:
        return []

    entries: list[ToolAuditLogEntry] = []
    for line in lines:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("type") != "message":
            continue
        msg = data.get("message", {})
        timestamp = data.get("timestamp", "")
        content = msg.get("content", [])

        if isinstance(content, list):
            for item in content:
                if item.get("type") == "toolCall":
                    args = item.get("arguments", {})
                    entries.append(ToolAuditLogEntry(
                        timestamp=timestamp,
                        tool_call_id=item.get("id", ""),
                        tool_name=item.get("name", ""),
                        phase="start",
                        session_id=session_id,
                        run_id=session_id,
                        session_key=session_id,
                        args=args if isinstance(args, dict) else {"raw": args},
                    ))

        if msg.get("role") == "toolResult":
            result_content = msg.get("content", [])
            details = msg.get("details", {})
            is_error = msg.get("isError", False)
            result_text = ""
            if isinstance(result_content, list):
                for item in result_content:
                    if item.get("type") == "text":
                        result_text = item.get("text", "")
                        break
            duration_ms = details.get("durationMs") if isinstance(details, dict) else None
            entries.append(ToolAuditLogEntry(
                timestamp=timestamp,
                tool_call_id=msg.get("toolCallId", ""),
                tool_name=msg.get("toolName", ""),
                phase="error" if is_error else "result",
                session_id=session_id,
                run_id=session_id,
                session_key=session_id,
                result=result_text,
                error=result_text if is_error else None,
                duration_ms=duration_ms,
            ))

    entries.sort(key=lambda e: e.timestamp)
    return entries


def read_session_total_tokens(session_id: str, docker: Optional[str] = None, docker_home: str = "/home/node") -> int:
    file_path = _resolve_session_file(session_id, docker, docker_home)
    lines = _read_file_lines(file_path, docker, docker_home=docker_home)
    if not lines:
        return 0

    provider = ""
    for line in lines:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = data.get("type")
        if t == "model_change":
            provider = data.get("provider", "") or ""
            break
        if t == "message":
            provider = data.get("provider", "") or data.get("message", {}).get("provider", "") or ""
            if provider:
                break

    if "matrixllm" in (provider or "").lower():
        return _estimate_tokens_via_tiktoken(lines)

    total = 0
    for line in lines:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        usage = data.get("message", {}).get("usage", {})
        if not isinstance(usage, dict):
            usage = data.get("usage", {}) if isinstance(data.get("usage"), dict) else {}
        total += usage.get("totalTokens", 0) or 0
    return total


def _estimate_tokens_via_tiktoken(lines: list[str]) -> int:
    try:
        import tiktoken
    except Exception:
        return 0
    try:
        enc = tiktoken.get_encoding("cl100k_base")
    except Exception:
        return 0

    def _encode(text: str) -> int:
        if not text:
            return 0
        try:
            return len(enc.encode(text, disallowed_special=()))
        except Exception:
            return max(1, len(text) // 4)

    total = 0
    for line in lines:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("type") != "message":
            continue
        msg = data.get("message", {})
        if not isinstance(msg, dict):
            continue
        content = msg.get("content", [])
        if isinstance(content, str):
            total += _encode(content)
            continue
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict):
                continue
            ctype = c.get("type")
            if ctype == "text":
                total += _encode(c.get("text", ""))
            elif ctype == "toolCall":
                total += _encode(c.get("name", ""))
                args = c.get("arguments")
                if args is not None:
                    try:
                        total += _encode(json.dumps(args, ensure_ascii=False))
                    except Exception:
                        total += _encode(str(args))
    return total
