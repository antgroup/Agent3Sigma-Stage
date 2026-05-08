"""Worker 进程：从共享队列动态取任务执行"""

import multiprocessing
import queue
import sys
import traceback

from openai import OpenAI

from .models import MultiTurnItem, MultiTurnResult
from .executor import execute_group, set_worker_id, _log


def worker_loop(
    worker_id: int,
    task_queue: multiprocessing.Queue,
    result_queue: multiprocessing.Queue,
    done_counter: multiprocessing.Value,
    config: dict,
) -> None:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
    set_worker_id(worker_id)
    image = config["docker"]["image"]
    num_runs = config["run"]["num_runs"]
    timeout = config["run"]["timeout"]
    judge_lang = config.get("judge_lang", "zh")

    target_config = {
        "base_url": config["target"]["base_url"],
        "api_key": config["target"]["api_key"],
        "model": config["target"]["model"],
    }

    judge_client = OpenAI(
        api_key=config["judge"]["api_key"],
        base_url=config["judge"]["base_url"],
    )
    judge_model = config["judge"]["model"]

    while True:
        try:
            group = task_queue.get(timeout=2)
        except queue.Empty:
            break

        seed, injected_items = group

        try:
            execute_group(
                seed, injected_items,
                image=image,
                worker_id=worker_id,
                num_runs=num_runs,
                target_config=target_config,
                judge_client=judge_client,
                judge_model=judge_model,
                judge_lang=judge_lang,
                timeout=timeout,
                result_queue=result_queue,
            )
        except Exception as e:
            traceback.print_exc()
            _log(f"group {seed.id} failed: {e}")
            result_queue.put([
                MultiTurnResult(
                    item=seed,
                    timestamp="",
                    overall_success=False,
                    overall_error=str(e),
                    reason=f"worker error: {e}",
                    duration=0.0,
                )
            ])

        with done_counter.get_lock():
            done_counter.value += 1
