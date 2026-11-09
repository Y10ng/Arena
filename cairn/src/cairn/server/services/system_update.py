"""系统一键更新：渠道检查、包准备与停服交接。

更新会停止 Server 自身，因此 apply 采用 detached 更新器交接：
Server 把交接信息原子写入 runtime/update-state.json 后立即派生
detached 的 deploy/apply_update.py，由它在 Server 退出后执行新版包内
的 deploy/update.sh 并把结果写回状态文件。状态文件位于 runtime/，
update.sh 保留该目录，重启后的 Server 通过它对账。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

import requests

from cairn import __version__
from cairn.server.services.dispatcher_config_store import DispatcherConfigStore
from cairn.server.services.time_utils import utcnow


# 状态机阶段；interrupted 表示 Server 重启后发现上次 prepare 未完成。
PHASE_IDLE = "idle"
PHASE_CHECKING = "checking"
PHASE_AVAILABLE = "available"
PHASE_UP_TO_DATE = "up_to_date"
PHASE_PREPARING = "preparing"
PHASE_READY = "ready"
PHASE_APPLYING = "applying"
PHASE_DONE = "done"
PHASE_FAILED = "failed"
PHASE_INTERRUPTED = "interrupted"

# applying 状态下更新器进程死亡后的宽限；期间视为仍在运行。
UPDATER_GRACE_SECONDS = 30

DEFAULT_CHANNEL_URL = "https://github.com/Y10ng/Arena/releases/latest/download/release.json"
CHANNEL_FORMAT = "arena-release-channel/v1"

STAGING_ROOT = Path.home() / ".arena-update-staging"

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {}
_PREPARE_THREAD: threading.Thread | None = None
_PREPARE_CANCEL = threading.Event()


class UpdateError(Exception):
    """更新流程的确定性失败，消息可直接展示给管理员。"""


def _state_path() -> Path:
    root = install_root()
    return root / "runtime" / "update-state.json"


def install_root() -> Path:
    """从本文件向上找同时包含应用源码与 deploy 脚本的安装根。

    不能用固定 parents[N]：services/ 子目录深度随实现变化。
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "cairn/pyproject.toml").is_file() and (parent / "deploy/update.sh").is_file():
            return parent
    raise UpdateError("无法定位 Arena 安装根：缺少 cairn/pyproject.toml 与 deploy/update.sh")


def install_type() -> str:
    """git 工作区不做一键更新；zip 安装以 deploy/update.sh 为准。"""
    root = install_root()
    if (root / ".git").exists() or (root / ".git").is_symlink():
        return "git"
    if (root / "deploy/update.sh").is_file():
        return "zip"
    return "unsupported"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _write_state(**fields: Any) -> None:
    _STATE.update(fields)
    _STATE["updated_at"] = utcnow()
    _atomic_write_json(_state_path(), dict(_STATE))


def read_state() -> dict[str, Any]:
    """读取状态并与现实对账；Server 重启后据此恢复 interrupted/failed。"""
    with _LOCK:
        path = _state_path()
        if not _STATE:
            if path.is_file():
                try:
                    _STATE.update(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    # 状态文件损坏不能阻塞管理页；回退 idle 并保留损坏文件供排查。
                    _STATE.update({"phase": PHASE_IDLE, "error": "状态文件损坏，已重置"})
            else:
                _STATE.update({"phase": PHASE_IDLE})
        _reconcile_locked()
        return dict(_STATE)


def _pid_alive(pid_entry: dict[str, Any] | None) -> bool:
    if not isinstance(pid_entry, dict):
        return False
    pid = pid_entry.get("pid")
    started = pid_entry.get("proc_start_time")
    if not isinstance(pid, int):
        return False
    try:
        stat_fields = (Path("/proc") / str(pid) / "stat").read_text(
            encoding="utf-8").rsplit(") ", 1)[1].split()
    except OSError:
        return False
    if not stat_fields or stat_fields[0] == "Z":
        return False
    if started is not None and (len(stat_fields) <= 19 or stat_fields[19] != str(started)):
        return False
    return True


def _reconcile_locked() -> None:
    """对账：重启残留、死亡更新器、取消请求。只在持锁时调用。"""
    phase = _STATE.get("phase")
    now = time.time()
    if phase == PHASE_APPLYING:
        if _pid_alive(_STATE.get("updater")):
            return
        finished_at = _STATE.get("apply_started_at")
        started = _parse_ts(finished_at)
        if started is not None and now - started < UPDATER_GRACE_SECONDS:
            return
        # 更新器已死且服务仍在响应（本函数正被调用即证明），说明它没跑完。
        _STATE["phase"] = PHASE_FAILED
        _STATE["error"] = "更新器进程意外退出；服务未受影响，可重新执行更新"
        _STATE["updated_at"] = utcnow()
        _atomic_write_json(_state_path(), dict(_STATE))
    elif phase in (PHASE_CHECKING, PHASE_PREPARING):
        # Server 重启后后台线程必然不存在；ready 阶段 staging 可能仍在。
        if phase == PHASE_PREPARING and _PREPARE_THREAD is not None and _PREPARE_THREAD.is_alive():
            return
        if phase == PHASE_CHECKING and _PREPARE_THREAD is not None and _PREPARE_THREAD.is_alive():
            return
        _STATE["phase"] = PHASE_INTERRUPTED
        _STATE["error"] = "Server 在准备阶段重启，准备未完成；可重新执行"
        _STATE["updated_at"] = utcnow()
        _atomic_write_json(_state_path(), dict(_STATE))


def _parse_ts(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        import datetime as _dt
        return _dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_dt.timezone.utc).timestamp()
    except ValueError:
        return None


def _proc_start_time(pid: int) -> str | None:
    try:
        fields = (Path("/proc") / str(pid) / "stat").read_text(
            encoding="utf-8").rsplit(") ", 1)[1].split()
    except OSError:
        return None
    return fields[19] if len(fields) > 19 else None


def parse_version(value: Any) -> tuple[int, int, int] | None:
    """严格 X.Y.Z；带预发布后缀或其他形态返回 None，调用方不得据此降级。"""
    if not isinstance(value, str):
        return None
    parts = value.strip().split(".")
    if len(parts) != 3:
        return None
    numbers: list[int] = []
    for part in parts:
        if not part.isdigit():
            return None
        numbers.append(int(part))
    return (numbers[0], numbers[1], numbers[2])


def channel_url() -> str:
    return os.environ.get("CAIRN_UPDATE_CHANNEL_URL", "").strip() or DEFAULT_CHANNEL_URL


def _requests_proxies() -> dict[str, str] | None:
    proxy = os.environ.get("CAIRN_UPDATE_PROXY", "").strip()
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def fetch_channel() -> dict[str, Any]:
    """拉取并校验渠道 release.json；返回规范化后的文档。"""
    url = channel_url()
    try:
        response = requests.get(url, timeout=(5, 20), proxies=_requests_proxies(),
                                headers={"Accept": "application/json"})
    except requests.exceptions.RequestException as exc:
        raise UpdateError(f"无法访问更新渠道 {url}: {exc}") from exc
    if response.status_code != 200:
        # v0.2.5 及更早 Release 没有 release.json；404 属预期状态而非故障。
        if response.status_code == 404:
            raise UpdateError("更新渠道暂无 release.json（该版本早于一键更新功能）")
        raise UpdateError(f"更新渠道返回 {response.status_code}")
    try:
        doc = response.json()
    except ValueError as exc:
        raise UpdateError("更新渠道响应不是有效 JSON") from exc
    return validate_channel_doc(doc)


def validate_channel_doc(doc: Any) -> dict[str, Any]:
    if not isinstance(doc, dict):
        raise UpdateError("release.json 顶层不是对象")
    if doc.get("format") != CHANNEL_FORMAT:
        raise UpdateError(f"release.json format 不是 {CHANNEL_FORMAT}")
    version = doc.get("version")
    if parse_version(version) is None:
        raise UpdateError(f"release.json 版本号不可解析: {version!r}")
    app = doc.get("app")
    if not isinstance(app, dict) or not isinstance(app.get("asset"), str):
        raise UpdateError("release.json 缺少 app.asset")
    if not isinstance(app.get("sha256"), str) or len(app["sha256"]) != 64:
        raise UpdateError("release.json app.sha256 非法")
    images = doc.get("images")
    gateway = images.get("traffic_gateway") if isinstance(images, dict) else None
    if not isinstance(gateway, dict) or not isinstance(gateway.get("image"), str):
        raise UpdateError("release.json 缺少 images.traffic_gateway.image")
    if not isinstance(gateway.get("digest"), str) or not gateway["digest"].startswith("sha256:"):
        raise UpdateError("release.json images.traffic_gateway.digest 非法")
    return doc


def _asset_url(channel_doc_url: str, asset: str) -> str:
    if not asset or "/" in asset or asset in (".", ".."):
        raise UpdateError("release.json asset 名非法")
    base = channel_doc_url.rsplit("/", 1)[0]
    return f"{base}/{asset}"


def _download(url: str, dest: Path, proxies: dict[str, str] | None) -> None:
    try:
        with requests.get(url, timeout=(5, 30), proxies=proxies, stream=True) as response:
            if response.status_code != 200:
                raise UpdateError(f"下载失败 {url}: HTTP {response.status_code}")
            with dest.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if _PREPARE_CANCEL.is_set():
                        raise UpdateError("已取消")
                    handle.write(chunk)
    except requests.exceptions.RequestException as exc:
        raise UpdateError(f"下载失败 {url}: {exc}") from exc


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_extract(zip_path: Path, dest: Path) -> None:
    """解压并拒绝路径逃逸；顶层目录必须是标准 arena-*-linux-code-only。"""
    with zipfile.ZipFile(zip_path) as archive:
        for name in archive.namelist():
            member = Path(name)
            if member.is_absolute() or ".." in member.parts:
                raise UpdateError(f"ZIP 内存在非法路径: {name}")
        archive.extractall(dest)


def _verify_staged(staged: Path) -> None:
    """复用发布包自带验收做硬门槛（stdlib-only，系统 python3 可运行）。"""
    script = staged / "deploy/verify_deployment.py"
    if not script.is_file():
        raise UpdateError("staged 包缺少 deploy/verify_deployment.py")
    result = subprocess.run(
        ["python3", str(script), "--root", str(staged), "--manifest-only"],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise UpdateError(f"发布包完整性验收失败: {detail[-1] if detail else result.returncode}")


def _repository(ref: str) -> str:
    """取镜像引用的 repository 部分（含 registry 主机），去掉 tag/digest。"""
    ref = ref.strip()
    if "@" in ref:
        ref = ref.split("@", 1)[0]
    if ":" in ref.rsplit("/", 1)[-1]:
        ref = ref.rsplit(":", 1)[0]
    return ref


def verify_gateway_image(image: str) -> None:
    """网关镜像离线检查（entrypoint + 脚本编译），与部署验收同源。"""
    script = install_root() / "deploy" / "verify_deployment.py"
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from verify_deployment import verify_gateway_image; "
        "verify_gateway_image(sys.argv[2]); print('ok')"
    )
    result = subprocess.run(
        ["python3", "-c", code, str(script.parent), image],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise UpdateError(f"网关镜像离线检查失败: {detail[-1] if detail else result.returncode}")


def _remote_digest(image: str) -> str:
    result = subprocess.run(
        ["docker", "image", "inspect", image, "--format", "{{index .RepoDigests 0}}"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise UpdateError("无法读取拉取镜像的远端 digest")
    value = result.stdout.strip()
    return value.split("@", 1)[1] if "@" in value else value


def _migrate_gateway_ref(channel_gateway_image: str) -> str | None:
    """把 dispatch.yaml 的同仓库网关引用切换为渠道 ref；返回旧值或 None。

    仅当现有 ref 的 repository 与渠道镜像一致时改写；第三方自定义引用不动。
    """
    raw_path = os.environ.get("CAIRN_DISPATCH_CONFIG", "").strip()
    if not raw_path:
        return None
    store = DispatcherConfigStore(Path(raw_path))
    try:
        config, _content, _revision = store.read()
    except Exception:
        return None
    container = config.get("container")
    if not isinstance(container, dict):
        return None
    current = container.get("traffic_gateway_image")
    if not isinstance(current, str) or _repository(current) != _repository(channel_gateway_image):
        return None
    if current == channel_gateway_image:
        return current
    container["traffic_gateway_image"] = channel_gateway_image
    # expected_revision=None：后台一次性迁移不做乐观锁；flock 已串行化写入。
    store.write_config(config, expected_revision=None)
    return current


def _prepare_worker(channel_doc: dict[str, Any], channel_doc_url: str) -> None:
    staging: Path | None = None
    try:
        version = channel_doc["version"]
        app = channel_doc["app"]
        gateway = channel_doc["images"]["traffic_gateway"]
        proxies = _requests_proxies()
        staging = Path(tempfile.mkdtemp(prefix=f"arena-{version}-", dir=STAGING_ROOT))
        _write_state(prepare_dir=str(staging), prepare_started_at=utcnow())

        zip_path = staging / app["asset"]
        _download(_asset_url(channel_doc_url, app["asset"]), zip_path, proxies)
        actual_sha = _sha256_file(zip_path)
        if actual_sha != app["sha256"].lower():
            raise UpdateError(f"ZIP SHA256 不匹配：期望 {app['sha256']}，实际 {actual_sha}")
        _write_state(prepare_progress=f"已下载并校验 {app['asset']}")

        sidecar_path = staging / f"{app['asset']}.sha256"
        if app.get("size") is not None:
            if zip_path.stat().st_size != int(app["size"]):
                raise UpdateError("ZIP 大小与 release.json 不一致")
        _download(_asset_url(channel_doc_url, f"{app['asset']}.sha256"), sidecar_path, proxies)
        sidecar_expected = sidecar_path.read_text(encoding="utf-8").strip().split()[0].lower()
        if sidecar_expected != actual_sha:
            raise UpdateError("sidecar 校验值与实际 SHA256 不一致")

        extract_dir = staging / "package"
        extract_dir.mkdir()
        _safe_extract(zip_path, extract_dir)
        entries = [item for item in extract_dir.iterdir() if item.is_dir()]
        if len(entries) != 1:
            raise UpdateError("ZIP 顶层目录结构不符合预期")
        staged = entries[0]
        for script in staged.glob("deploy/*.sh"):
            script.chmod(0o755)
        for script in staged.glob("deploy/*.py"):
            script.chmod(0o755)
        if not (staged / "deploy/update.sh").is_file():
            raise UpdateError("发布包缺少 deploy/update.sh")
        _write_state(prepare_progress="已解压并校验发布清单")
        _verify_staged(staged)

        if _PREPARE_CANCEL.is_set():
            raise UpdateError("已取消")
        _write_state(prepare_progress=f"拉取网关镜像 {gateway['image']}")
        pull = subprocess.Popen(
            ["docker", "pull", gateway["image"]],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        try:
            _return = pull.wait(timeout=1800)
        except subprocess.TimeoutExpired:
            pull.terminate()
            raise UpdateError("docker pull 超时") from None
        _stderr = pull.stderr.read().decode("utf-8", "replace") if pull.stderr else ""
        if _return != 0:
            detail = _stderr.strip().splitlines()
            raise UpdateError(f"docker pull 失败: {detail[-1] if detail else _return}")
        digest = _remote_digest(gateway["image"])
        if digest != gateway["digest"]:
            raise UpdateError(
                f"网关镜像 digest 不匹配：期望 {gateway['digest']}，实际 {digest}")
        verify_gateway_image(gateway["image"])

        previous_ref = _migrate_gateway_ref(gateway["image"])
        _write_state(
            phase=PHASE_READY,
            prepare_progress="准备完成",
            channel_version=version,
            staged_dir=str(staged),
            gateway_image=gateway["image"],
            gateway_digest=gateway["digest"],
            previous_gateway_ref=previous_ref,
            error=None,
        )
    except UpdateError as exc:
        with _LOCK:
            if _PREPARE_CANCEL.is_set():
                _write_state(phase=PHASE_AVAILABLE, prepare_progress=None,
                             error=str(exc))
            else:
                _write_state(phase=PHASE_FAILED, error=str(exc))
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
    except Exception as exc:  # 未预期异常同样落状态，不让线程静默死亡
        with _LOCK:
            _write_state(phase=PHASE_FAILED, error=f"准备阶段意外失败: {type(exc).__name__}: {exc}")
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


def start_check() -> None:
    """同步执行渠道检查（网络往返短，不引入后台状态）。"""
    with _LOCK:
        phase = _STATE.get("phase", PHASE_IDLE)
        if phase in (PHASE_PREPARING, PHASE_APPLYING, PHASE_READY):
            raise UpdateError(f"当前状态 {phase} 不允许检查")
    doc = fetch_channel()
    channel_version = doc["version"]
    with _LOCK:
        comparison = compare_versions(__version__, channel_version)
        if comparison < 0:
            _write_state(phase=PHASE_AVAILABLE, channel=doc, channel_doc_url=channel_url(),
                         channel_version=channel_version, error=None)
        elif comparison == 0:
            _write_state(phase=PHASE_UP_TO_DATE, channel=doc, channel_doc_url=channel_url(),
                         channel_version=channel_version, error=None,
                         prepare_progress=None)
        else:
            # 本地版本比渠道新：开发环境或渠道回退，不提供更新入口。
            _write_state(phase=PHASE_IDLE, channel=doc, channel_doc_url=channel_url(),
                         channel_version=channel_version,
                         error=f"渠道版本 {channel_version} 不高于当前版本 {__version__}")


def compare_versions(current: str, channel: str) -> int:
    current_tuple = parse_version(current)
    channel_tuple = parse_version(channel)
    if current_tuple is None or channel_tuple is None:
        raise UpdateError(f"版本号不可比较: {current!r} vs {channel!r}")
    if current_tuple < channel_tuple:
        return -1
    if current_tuple > channel_tuple:
        return 1
    return 0


def start_prepare() -> None:
    with _LOCK:
        if _STATE.get("phase") != PHASE_AVAILABLE:
            raise UpdateError(f"当前状态 {_STATE.get('phase')} 不允许准备")
        channel_doc = _STATE.get("channel")
        channel_doc_url = _STATE.get("channel_doc_url")
        if not isinstance(channel_doc, dict) or not isinstance(channel_doc_url, str):
            raise UpdateError("缺少渠道信息，请先检查更新")
        _PREPARE_CANCEL.clear()
        thread = threading.Thread(
            target=_prepare_worker, args=(channel_doc, channel_doc_url),
            name="cairn-update-prepare", daemon=True,
        )
        global _PREPARE_THREAD
        _PREPARE_THREAD = thread
        _write_state(phase=PHASE_PREPARING, error=None, prepare_progress=None)
    thread.start()


def cancel_prepare() -> None:
    with _LOCK:
        if _STATE.get("phase") != PHASE_PREPARING:
            raise UpdateError("当前没有进行中的准备")
        _PREPARE_CANCEL.set()


@dataclass
class HandoverResult:
    updater_pid: int


def apply() -> HandoverResult:
    """执行一键更新：写交接信息、派生 detached 更新器并立即返回。"""
    with _LOCK:
        if _STATE.get("phase") != PHASE_READY:
            raise UpdateError(f"当前状态 {_STATE.get('phase')} 不允许执行更新")
        kind = install_type()
        if kind == "git":
            raise UpdateError("Git 源码安装不支持一键更新，请按源码更新步骤操作")
        if kind != "zip":
            raise UpdateError("当前安装类型不支持一键更新")
        staged = _STATE.get("staged_dir")
        if not isinstance(staged, str) or not (Path(staged) / "deploy/apply_update.py").is_file():
            raise UpdateError("staged 包缺少 deploy/apply_update.py，无法交接")

        root = install_root()
        endpoint_file = root / "runtime/pids/server.endpoint"
        host = port = None
        if endpoint_file.is_file():
            lines = endpoint_file.read_text(encoding="utf-8").split()
            if len(lines) >= 2:
                host, port = lines[0], lines[1]
        handover = {
            "staged_dir": staged,
            "target_root": str(root),
            "host": host,
            "port": port,
            "log_path": str(root / "runtime/logs/update-apply.log"),
        }
        _write_state(phase=PHASE_APPLYING, handover=handover, error=None,
                     apply_started_at=utcnow())
        state_file = _state_path()
        apply_script = Path(staged) / "deploy/apply_update.py"

    # 在锁外派生进程；清理 CAIRN_* 避免旧进程环境泄漏进更新流程。
    env = {key: value for key, value in os.environ.items() if not key.startswith("CAIRN_")}
    env.setdefault("HOME", str(Path.home()))
    process = subprocess.Popen(
        ["python3", str(apply_script), "--state", str(state_file)],
        cwd="/", env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    with _LOCK:
        _write_state(
            updater={"pid": process.pid, "proc_start_time": _proc_start_time(process.pid)},
        )
    return HandoverResult(updater_pid=process.pid)
