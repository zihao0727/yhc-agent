"""Run untrusted Python in a disposable Linux container, never via host Python."""
import json
import subprocess
import threading
import time
import uuid

from .config import settings

OUTPUT_LIMIT = 2 * 1024 * 1024


def docker_command(arguments, timeout=8):
    return subprocess.run(["docker", *arguments], capture_output=True, timeout=timeout,
                          encoding="utf-8", errors="replace",
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def availability():
    if not settings().file_workspace_enabled:
        return {"available": False, "reason": "文件脚本工作区已禁用"}
    try:
        info = docker_command(["info", "--format", "{{.OSType}}"])
        if info.returncode or info.stdout.strip() != "linux":
            return {"available": False, "reason": "需要可用的Docker Linux引擎"}
        image = docker_command(["image", "inspect", settings().file_workspace_image, "--format", "{{.Id}}"])
        if image.returncode:
            return {"available": False, "reason": "文件脚本隔离镜像尚未构建"}
        return {"available": True, "image_id": image.stdout.strip()}
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False, "reason": "Docker不可用或连接超时"}


def run_python(input_dir, check=lambda: None):
    ready = availability()
    if not ready["available"]:
        return {"ok": False, "cause": "service_unavailable", "error": ready["reason"],
                "next_action": "保留原始资料并报告运行环境问题；不要重复执行同一脚本"}
    name = "firefly-file-" + uuid.uuid4().hex
    # Mount only this invocation's copied inputs, never the repository, Docker socket or env.
    command = ["docker", "run", "--rm", "--pull=never", "--name", name,
               "--network=none", "--read-only", "--cap-drop=ALL",
               "--security-opt=no-new-privileges", "--pids-limit=32", "--cpus=1",
               f"--memory={settings().file_workspace_memory_mb}m", "--memory-swap="
               f"{settings().file_workspace_memory_mb}m", "--user=65534:65534",
               "--log-driver=none", "--ulimit=nofile=128:128",
               "--tmpfs=/work:rw,noexec,nosuid,nodev,size=64m,mode=1777",
               "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=32m,mode=1777",
               "--mount", f"type=bind,source={input_dir.resolve()},target=/input,readonly",
               ready["image_id"]]
    output, exceeded = bytearray(), threading.Event()
    process = reader = None

    def consume(pipe):
        while chunk := pipe.read(8192):
            remaining = OUTPUT_LIMIT - len(output)
            output.extend(chunk[:max(remaining, 0)])
            if len(chunk) > remaining:
                exceeded.set()

    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        reader = threading.Thread(target=consume, args=(process.stdout,), daemon=True)
        reader.start()
        deadline = time.monotonic() + settings().file_workspace_timeout
        while process.poll() is None:
            check()
            if exceeded.is_set() or time.monotonic() >= deadline:
                return {"ok": False, "cause": "execution_error",
                        "error": "脚本输出超限" if exceeded.is_set() else "脚本执行超时",
                        "next_action": "缩小处理范围或修正脚本，再执行"}
            time.sleep(.1)
        reader.join(timeout=2)
        if reader.is_alive() or exceeded.is_set():
            return {"ok": False, "cause": "execution_error", "error": "脚本输出超限或未结束"}
        if process.returncode:
            return {"ok": False, "cause": "execution_error", "error": output.decode("utf-8", "replace")[-3000:]}
        try:
            result = json.loads(output)
            if not isinstance(result, dict) or type(result.get("ok")) is not bool:
                raise ValueError("invalid envelope")
            return {**result, "image_id": ready["image_id"]}
        except (ValueError, UnicodeError):
            return {"ok": False, "cause": "execution_error",
                    "error": "脚本没有返回有效JSON；将结果写入/work/result.json"}
    finally:
        # Also removes descendants on timeout/cancellation. No untrusted host process fallback.
        try:
            docker_command(["rm", "-f", name], timeout=8)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            if reader is not None:
                reader.join(timeout=2)
            if process.stdout:
                process.stdout.close()
