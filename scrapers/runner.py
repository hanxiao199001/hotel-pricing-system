"""调用独立的携程采集脚本（子进程）。

采集脚本依赖 playwright，运行在单独的 venv 里，且必须有头浏览器 + 人工登录/验证，
因此不用进程内 import，而是子进程调用：脚本崩溃或被杀不影响主流程。
"""
import json
import os
import subprocess
import tempfile


class ScraperError(RuntimeError):
    """采集失败：脚本缺失、超时、非零退出、输出无法解析。"""


def build_command(cfg: dict, place: str, city: str, distance_m: int,
                  checkin: str, checkout: str, out_path: str) -> list:
    """组装采集脚本的命令行参数。单独抽出来便于测试，不必真跑浏览器。"""
    cmd = [
        cfg["python"],
        cfg["script"],
        "--place", place,
        "--city", city,
        "--distance", str(int(distance_m)),
        "--checkin", checkin,
        "--checkout", checkout,
        "--profile", cfg["profile"],
        "--channel", cfg["channel"],
        "--cdp-port", str(cfg["cdp_port"]),
        "--login-wait", str(cfg["login_wait"]),
        "--out", out_path,
    ]
    cmd.append("--collect-rooms" if cfg.get("collect_rooms") else "--no-collect-rooms")
    return cmd


def _decode(raw: bytes) -> str:
    if not raw:
        return ""
    # 采集脚本已把 stdout/stderr 强制为 utf-8
    return raw.decode("utf-8", "replace")


def run_scraper(cfg: dict, place: str, city: str, distance_m: int,
                checkin: str, checkout: str) -> dict:
    """执行一次采集，返回脚本产出的原始 dict（附加 _stdout 便于排障）。"""
    script = cfg.get("script") or ""
    python = cfg.get("python") or ""

    if not os.path.isfile(python):
        raise ScraperError(
            f"采集脚本的解释器不存在: {python}\n"
            f"    提示: 该解释器需要安装 playwright，可用环境变量 SCRAPER_PYTHON 指定。"
        )
    if not os.path.isfile(script):
        raise ScraperError(
            f"采集脚本不存在: {script}\n"
            f"    提示: 可用环境变量 CTRIP_SCRAPER_SCRIPT 指定脚本绝对路径。"
        )

    fd, out_path = tempfile.mkstemp(prefix="ctrip_scrape_", suffix=".json")
    os.close(fd)
    try:
        cmd = build_command(cfg, place, city, distance_m, checkin, checkout, out_path)
        timeout = int(cfg.get("timeout") or 900)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout,
                cwd=os.path.dirname(os.path.abspath(script)),
            )
        except subprocess.TimeoutExpired as exc:
            raise ScraperError(
                f"采集超时（超过 {timeout} 秒）。可调大 SCRAPER_TIMEOUT，"
                f"或把 SCRAPER_COLLECT_ROOMS 设为 false 跳过详情页。"
            ) from exc
        except OSError as exc:
            raise ScraperError(f"无法启动采集脚本: {exc}") from exc

        stdout = _decode(proc.stdout)
        stderr = _decode(proc.stderr)

        if proc.returncode != 0:
            tail = (stderr or stdout).strip()[-500:]
            raise ScraperError(f"采集脚本退出码 {proc.returncode}。\n{tail}")

        if not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
            tail = (stdout or stderr).strip()[-500:]
            raise ScraperError(f"采集脚本未产出结果文件。\n{tail}")

        try:
            with open(out_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            raise ScraperError(f"无法解析采集结果: {exc}") from exc

        data["_stdout"] = stdout[-2000:]
        if stderr.strip():
            data["_stderr"] = stderr[-1000:]
        return data
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass
