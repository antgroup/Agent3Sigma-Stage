"""Docker 容器生命周期管理"""

import json
import os
import re
import subprocess
import tempfile
import time
from typing import Optional
from urllib.parse import urlparse


DOCKER_HOME = "/home/node"
CONTAINER_PREFIX = "agent3sigma-"


def _provider_key_from_url(base_url: str) -> str:
    host = ""
    try:
        host = urlparse(base_url).hostname or ""
    except Exception:
        pass
    slug = host.replace(".", "-") if host else "provider"
    return f"custom-{slug}"


def _build_default_config(base_url: str, api_key: str, model: str) -> dict:
    provider_key = _provider_key_from_url(base_url) if base_url else "custom-provider"
    model_id = model or "default-model"
    return {
        "models": {
            "mode": "merge",
            "providers": {
                provider_key: {
                    "baseUrl": base_url or "",
                    "apiKey": api_key or "",
                    "api": "openai-completions",
                    "models": [{
                        "id": model_id,
                        "name": f"{model_id} (Custom Provider)",
                        "reasoning": False,
                        "input": ["text"],
                        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                        "contextWindow": 16000,
                        "maxTokens": 4096,
                    }],
                }
            },
        },
        "agents": {
            "defaults": {
                "model": {"primary": f"{provider_key}/{model_id}"},
                "models": {f"{provider_key}/{model_id}": {}},
            }
        },
        "commands": {"native": "auto", "nativeSkills": "auto", "restart": True, "ownerDisplay": "raw"},
        "plugins": {"allow": ["benchmark-mock"]},
        "gateway": {"mode": "local", "auth": {"mode": "token", "token": "placeholder"}},
    }


def _apply_config_overrides(config: dict, base_url: str, api_key: str, target_model: str) -> bool:
    providers = config.setdefault("models", {}).setdefault("providers", {})

    if base_url:
        template = next(iter(providers.values())) if providers else {}
        new_key = _provider_key_from_url(base_url)
        model_id = target_model or (
            template.get("models", [{}])[0].get("id") if template.get("models") else "default-model"
        )
        models_cfg = template.get("models") or [{
            "id": model_id,
            "name": f"{model_id} (Custom Provider)",
            "reasoning": False,
            "input": ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 160000,
            "maxTokens": 40960,
        }]
        providers.clear()
        providers[new_key] = {
            "baseUrl": base_url,
            "apiKey": api_key or template.get("apiKey", ""),
            "api": template.get("api", "openai-completions"),
            "models": models_cfg,
        }
        provider_key = new_key
        provider = providers[new_key]
    else:
        if not providers:
            return False
        provider_key = next(iter(providers))
        provider = providers[provider_key]
        if api_key:
            provider["apiKey"] = api_key

    if target_model:
        if provider.get("models"):
            provider["models"][0]["id"] = target_model
            provider["models"][0]["name"] = f"{target_model} (Custom Provider)"
        model_ref = f"{provider_key}/{target_model}"
        agents = config.setdefault("agents", {}).setdefault("defaults", {})
        agents["model"] = {"primary": model_ref}
        agents["models"] = {model_ref: {}}

    return True


def inject_config(container_id: str, base_url: str, api_key: str, target_model: str) -> None:
    if not base_url and not api_key and not target_model:
        return

    config_path = f"{DOCKER_HOME}/.openclaw/openclaw.json"

    tmp_pre = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            tmp_pre = tmp.name
        cp_res = subprocess.run(
            ["docker", "cp", f"{container_id}:{config_path}", tmp_pre],
            capture_output=True, text=True, timeout=10,
        )
        if cp_res.returncode == 0:
            with open(tmp_pre) as f:
                config = json.load(f)
        else:
            config = _build_default_config(base_url, api_key, target_model)
    except Exception:
        config = _build_default_config(base_url, api_key, target_model)
    finally:
        if tmp_pre and os.path.exists(tmp_pre):
            os.unlink(tmp_pre)

    if not _apply_config_overrides(config, base_url, api_key, target_model):
        return

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
            json.dump(config, tmp, ensure_ascii=False, indent=2)
            tmp_path = tmp.name
        os.chmod(tmp_path, 0o644)
        subprocess.run(
            ["docker", "cp", tmp_path, f"{container_id}:{config_path}"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as e:
        print(f"  inject config failed: {e}", flush=True)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def create_container(
    image: str,
    worker_id: int,
    base_url: str = "",
    api_key: str = "",
    target_model: str = "",
) -> str:
    container_name = f"{CONTAINER_PREFIX}w{worker_id}-{int(time.time())}-{os.getpid()}"

    cmd = ["docker", "create", "--name", container_name, image]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"docker create failed: {result.stderr.strip()}")
    container_id = result.stdout.strip()[:12]

    inject_config(container_id, base_url, api_key, target_model)

    result = subprocess.run(
        ["docker", "start", container_id],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        subprocess.run(["docker", "rm", "-f", container_id],
                       capture_output=True, text=True, timeout=15)
        raise RuntimeError(f"docker start failed: {result.stderr.strip()}")

    ready = False
    for _ in range(15):
        time.sleep(1)
        probe = subprocess.run(
            ["docker", "exec", container_id, "true"],
            capture_output=True, text=True, timeout=5,
        )
        if probe.returncode == 0:
            ready = True
            break
    if not ready:
        subprocess.run(["docker", "rm", "-f", container_id],
                       capture_output=True, text=True, timeout=30)
        raise RuntimeError(f"container {container_id} not ready after 15s")

    return container_id


def destroy_container(container_id: str) -> None:
    try:
        subprocess.run(
            ["docker", "rm", "-f", container_id],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        pass


def cleanup_stale_containers() -> None:
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name={CONTAINER_PREFIX}",
             "--format", "{{.ID}} {{.Names}}"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return
        for line in result.stdout.strip().splitlines():
            parts = line.split()
            cid, cname = parts[0], parts[1] if len(parts) > 1 else parts[0]
            segments = cname.split("-")
            if len(segments) >= 3:
                try:
                    creator_pid = int(segments[-1])
                    os.kill(creator_pid, 0)
                    continue
                except (ValueError, ProcessLookupError):
                    pass
                except PermissionError:
                    continue
            else:
                continue
            subprocess.run(["docker", "rm", "-f", cid],
                           capture_output=True, text=True, timeout=15)
    except Exception:
        pass


def _extract_setup_top_dirs(setup_commands: list[str]) -> list[str]:
    abs_path_re = re.compile(r'(?<![a-zA-Z0-9_])(/(?:tmp|home|var|opt|etc|srv)[a-zA-Z0-9_./@{}\-]*)')
    home_path_re = re.compile(r'(~[a-zA-Z0-9_./\-]*)')
    heredoc_re = re.compile(r'<<-?\s*[\'"]?\w+[\'"]?')

    top_dirs: set[str] = set()
    for cmd in setup_commands:
        cmd_line = cmd.split("\n")[0] if "\n" in cmd else cmd
        m = heredoc_re.search(cmd_line)
        if m:
            cmd_line = cmd_line[:m.end()]

        for match in abs_path_re.findall(cmd_line):
            path = match.rstrip("'\"")
            parts = path.split("/")
            if len(parts) >= 3:
                top_dirs.add("/".join(parts[:3]))
            else:
                top_dirs.add(path)
        for match in home_path_re.findall(cmd_line):
            path = match.rstrip("'\"")
            parts = path.split("/")
            if len(parts) >= 2:
                top_dirs.add("/".join(parts[:2]))
            else:
                top_dirs.add(path)

    exclude = {"/dev", "/proc", "/sys", "/etc", "/var", "/opt", "/srv", "/home", "/tmp"}
    return sorted(d for d in top_dirs if d not in exclude and d != "/")


def _docker_cp_skill(container_id: str, src: str, dst: str, timeout: int = 30) -> tuple[bool, str]:
    try:
        subprocess.run(
            ["docker", "exec", "-u", "root", "-e", f"HOME={DOCKER_HOME}",
             container_id, "bash", "-c", f"mkdir -p {dst}"],
            capture_output=True, text=True, timeout=timeout,
        )
        result = subprocess.run(
            ["docker", "exec", "-u", "root", "-e", f"HOME={DOCKER_HOME}",
             container_id, "bash", "-c", f"echo {dst}"],
            capture_output=True, text=True, timeout=timeout,
        )
        expanded_dst = result.stdout.strip()
        if not expanded_dst:
            return False, "Failed to resolve destination path"
        result = subprocess.run(
            ["docker", "cp", src, f"{container_id}:{expanded_dst}"],
            capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode == 0, result.stdout or result.stderr or ""
    except Exception as e:
        return False, str(e)


def run_shell(container_id: str, command: str, timeout: int = 30) -> tuple[bool, str]:
    try:
        cp_match = re.match(
            r'cp\s+-r\s+(data/\S+)\s+(\S+)',
            command.strip(),
        )
        if cp_match:
            return _docker_cp_skill(container_id, cp_match.group(1), cp_match.group(2), timeout)
        full_cmd = [
            "docker", "exec", "-u", "root",
            "-e", f"HOME={DOCKER_HOME}",
            container_id, "bash", "-c", command,
        ]
        result = subprocess.run(full_cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode == 0, result.stdout or result.stderr or ""
    except Exception as e:
        return False, str(e)


def run_setup(container_id: str, setup_commands: list[str]) -> bool:
    for cmd in setup_commands:
        cmd = cmd.replace('sudo', '')
        success, output = run_shell(container_id, cmd, timeout=30)
        if not success:
            print(f"  setup failed: {cmd[:60]}...", flush=True)
            if output:
                print(f"  detail: {output.strip()[:200]}", flush=True)
            return False
    top_dirs = _extract_setup_top_dirs(setup_commands)
    if top_dirs:
        resolved = [d.replace("~", DOCKER_HOME) for d in top_dirs]
        paths_str = " ".join(resolved)
        run_shell(container_id, f"chown -R node:node {paths_str}", timeout=30)
    return True


def run_cleanup(container_id: str, setup_commands: list[str]) -> None:
    paths_to_clean = set()
    for cmd in setup_commands:
        if cmd.strip().startswith("mkdir"):
            parts = cmd.strip().split()
            for p in parts[1:]:
                if p.startswith("/") and p != "-p":
                    paths_to_clean.add(p)
        elif ">" in cmd:
            parts = cmd.split(">")
            if len(parts) >= 2:
                tokens = parts[1].strip().split()
                if tokens:
                    path = tokens[0].strip("'\"")
                    if path.startswith("/"):
                        paths_to_clean.add(path)
    for path in paths_to_clean:
        run_shell(container_id, f"rm -rf {path}", timeout=10)


def run_agent(
    container_id: str,
    session_id: str,
    message: str,
    timeout: int = 600,
) -> tuple[str, bool, Optional[str]]:
    cmd = [
        "docker", "exec", container_id,
        "openclaw", "agent",
        "--session-id", session_id,
        "--message", message,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        output = result.stdout or ""
        if result.returncode != 0:
            return output, False, f"Exit code {result.returncode}: {result.stderr or ''}"
        return output, True, None
    except subprocess.TimeoutExpired as e:
        output = ""
        if e.stdout:
            output = e.stdout.decode() if isinstance(e.stdout, bytes) else e.stdout
        return output, False, f"Timeout ({timeout}s)"
    except Exception as e:
        return "", False, str(e)
