"""
115 网盘本地文件秒传插件（MoviePilot V3）。

功能：
1. 115 账号支持 CK Cookie 登录，并支持扫码登录自动获取 Cookie 填入；
2. 网盘目录树（含全部普通文件夹）明文可见、可浏览选择；
3. 可选择本地整个文件夹，批量对其中媒体/文件尝试秒传；
4. 监控 qBittorrent 下载完成，秒级尝试秒传；秒传成功且该文件还在 MoviePilot
   整理队列时，把它从整理队列移除（不再由 MP 整理）；整理成功的文件不再秒传；
5. 定时重试"秒传失败且尚未被整理成功"的文件；
6. 全程只读本地文件，绝不移动、改名、删除本地目录或文件。

实现约定：
- 使用官方维护的 p115client 完成 Cookie 登录、扫码登录、目录遍历、秒传(含二次校验)。
- 秒传只做"快传"(reuse)，未命中时绝不把文件本体上传到 OSS，直接判为失败交重试。
- CK / 扫码会话 token 属于敏感信息：只在服务端保存，不回传前端、不写日志。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from app.core.event import Event, eventmanager
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType

from .p115_core import P115AuthError, P115Error, P115Service

LOG_TAG = "[115秒传] "

# 可选：直接从 MoviePilot 的媒体库配置读取允许的媒体扩展名
_DEFAULT_MEDIA_EXTS = {
    "mp4", "mkv", "avi", "mov", "wmv", "flv", "ts", "m2ts", "iso",
    "rmvb", "webm", "mpg", "mpeg", "m4v", "vob", "3gp", "strm",
    "srt", "ass", "ssa", "sub", "idx", "sup", "vtt",
    "mp3", "flac", "aac", "m4a", "ape", "wav", "dts",
}


class Mp115CloudPush(_PluginBase):
    """115 网盘秒传插件。"""

    plugin_name = "硬盘秒传"
    plugin_desc = (
        "115 网盘本地文件秒传：CK/扫码登录、目录树浏览、选择本地整个文件夹批量秒传、"
        "监控 qB 下载完成秒级秒传并从 MP 整理队列移除、定时重试未成功且未整理的文件。"
        "全程只读本地文件，绝不移动/删除源文件。"
    )
    plugin_icon = "115pan_A.png"
    plugin_version = "1.0.1"
    plugin_author = "minis"
    author_url = ""
    plugin_config_prefix = "mp115cloudpush_"
    plugin_order = 40
    auth_level = 1

    # ---- 配置项默认值 ----
    _enabled = False
    _cookie = ""                  # 115 Cookie（只服务端保存）
    _remote_path = ""             # 目标网盘路径，如 /秒传 或 空=根目录
    _watch_paths = ""             # 手动选择监控的本地目录(可多行)
    _extensions = ""              # 逗号分隔的扩展名，空=内置默认
    _qb_enabled = True            # 监控 qB 下载完成
    _qb_poll_interval = 15        # qB 轮询间隔（秒）
    _retry_enabled = True         # 定时重试失败且未整理的文件
    _retry_interval = 600         # 重试间隔（秒）
    _remove_queue = True          # 秒传成功且仍在 MP 整理队列时移除
    _notify = True                # 发送通知

    # ---- 运行时 ----
    _service: Optional[P115Service] = None
    _lock = threading.Lock()
    _pending_retries: Dict[str, Dict[str, Any]] = {}   # path -> {attempts,next_retry,last_error}
    _done: Set[str] = set()        # 已成功秒传的文件路径快照（相对路径键）
    _history: List[Dict[str, Any]] = []
    _running = False
    _qr_session: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def init_plugin(self, config: dict = None) -> None:
        """读取配置并建立运行状态。"""
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._cookie = (config.get("cookie") or self._cookie or "").strip()
        self._remote_path = (config.get("remote_path") or "").strip()
        self._watch_paths = (config.get("watch_paths") or "").strip()
        self._extensions = (config.get("extensions") or "").strip()
        self._qb_enabled = bool(config.get("qb_enabled", self._qb_enabled))
        self._qb_poll_interval = max(int(config.get("qb_poll_interval") or 15), 5)
        self._retry_enabled = bool(config.get("retry_enabled", self._retry_enabled))
        self._retry_interval = max(int(config.get("retry_interval") or 600), 30)
        self._remove_queue = bool(config.get("remove_queue", self._remove_queue))
        self._notify = bool(config.get("notify", True))

        # 重建服务对象（Cookie 变化时自动重置）
        if self._service is None or self._service.cookie != self._cookie:
            self._service = P115Service(cookie=self._cookie)

        # 载入持久化状态
        self._done = set(self.get_data("done") or [])
        self._history = list(self.get_data("history") or [])
        pending = self.get_data("retry_queue") or []
        self._pending_retries = {
            p["path"]: p for p in pending if isinstance(p, dict) and p.get("path")
        }

    def get_state(self) -> bool:
        """返回插件是否启用。"""
        return bool(self._enabled)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册远程命令。"""
        return [
            {
                "cmd": "/mp115_status",
                "event": EventType.PluginAction,
                "desc": "查看 115 秒传状态",
                "category": "插件",
                "data": {"action": "mp115_status"},
            }
        ]

    def get_render_mode(self) -> Tuple[str, Optional[str]]:
        """使用 Vuetify 配置/详情页。"""
        return "vuetify", None

    def get_api(self) -> List[Dict[str, Any]]:
        """注册插件 API（目录浏览、扫码、批量秒传）。"""
        return [
            {
                "path": "/check_login", "endpoint": self.api_check_login,
                "methods": ["GET"], "auth": "bear", "summary": "检查 115 登录态",
            },
            {
                "path": "/tree", "endpoint": self.api_tree,
                "methods": ["GET"], "auth": "bear", "summary": "网盘目录树",
            },
            {
                "path": "/children", "endpoint": self.api_children,
                "methods": ["GET"], "auth": "bear", "summary": "目录子项",
            },
            {
                "path": "/qr/start", "endpoint": self.api_qr_start,
                "methods": ["POST"], "auth": "bear", "summary": "发起扫码登录",
            },
            {
                "path": "/qr/poll", "endpoint": self.api_qr_poll,
                "methods": ["POST"], "auth": "bear", "summary": "轮询扫码状态",
            },
            {
                "path": "/push_path", "endpoint": self.api_push_path,
                "methods": ["POST"], "auth": "bear", "summary": "手动秒传本地路径",
            },
            {
                "path": "/retry/now", "endpoint": self.api_retry_now,
                "methods": ["POST"], "auth": "bear", "summary": "立即重试失败队列",
            },
            {
                "path": "/status", "endpoint": self.api_status,
                "methods": ["GET"], "auth": "bear", "summary": "插件状态与历史",
            },
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """注册周期任务：qB 下载完成轮询 + 失败重试。"""
        if not self.get_state():
            return []
        services = []
        if self._qb_enabled:
            services.append({
                "id": f"{self.__class__.__name__}.Qb",
                "name": "qB 下载完成秒传",
                "trigger": "interval",
                "func": self.tick_qb,
                "kwargs": {"seconds": self._qb_poll_interval, "coalesce": True,
                           "max_instances": 1},
            })
        if self._retry_enabled:
            services.append({
                "id": f"{self.__class__.__name__}.Retry",
                "name": "失败秒传重试",
                "trigger": "interval",
                "func": self.tick_retry,
                "kwargs": {"seconds": self._retry_interval, "coalesce": True,
                           "max_instances": 1},
            })
        return services

    def stop_service(self) -> None:
        """停用插件，保存持久化状态。"""
        self._enabled = False
        self._persist()

    # ------------------------------------------------------------------
    # API 实现
    # ------------------------------------------------------------------
    def _service_or_raise(self) -> P115Service:
        if self._service is None:
            self._service = P115Service()
        return self._service

    def api_check_login(self) -> Dict[str, Any]:
        """检查 115 Cookie 登录态。"""
        svc = self._service_or_raise()
        if not svc.cookie:
            return {"ok": False, "message": "未配置 Cookie"}
        try:
            uid = svc.user_id
            return {"ok": True, "logged": uid is not None, "uid": uid}
        except P115AuthError as e:
            return {"ok": False, "message": str(e)}
        except P115Error as e:
            return {"ok": False, "message": str(e)}

    def api_tree(self) -> Dict[str, Any]:
        """返回网盘目录树（只包含目录节点）。"""
        svc = self._service_or_raise()
        try:
            tree = svc.tree()
            return {"ok": True, "tree": tree}
        except P115AuthError as e:
            return {"ok": False, "message": str(e)}
        except P115Error as e:
            return {"ok": False, "message": str(e)}

    def api_children(self, cid: int = 0) -> Dict[str, Any]:
        """返回某目录的子项（目录+文件）。"""
        svc = self._service_or_raise()
        try:
            data = svc.list_children(int(cid or 0))
            return {"ok": True, **data}
        except P115AuthError as e:
            return {"ok": False, "message": str(e)}
        except P115Error as e:
            return {"ok": False, "message": str(e)}

    def api_qr_start(self) -> Dict[str, Any]:
        """发起扫码登录。"""
        svc = self._service_or_raise()
        try:
            sess = svc.qr_start()
            self._qr_session = sess
            return {"ok": True, "url": sess["url"],
                    "expires": sess["expires"]}
        except P115Error as e:
            return {"ok": False, "message": str(e)}

    def api_qr_poll(self) -> Dict[str, Any]:
        """轮询扫码状态；成功后 Cookie 自动填入（仅服务端）。"""
        svc = self._service_or_raise()
        if not self._qr_session:
            return {"ok": False, "message": "尚无扫码会话"}
        try:
            r = svc.qr_poll(self._qr_session)
        except P115Error as e:
            return {"ok": False, "message": str(e)}
        if r.get("status") == "ok":
            # Cookie 已保存在服务对象；回写配置供重启使用
            self._persist_cookie()
            self._qr_session = None
            return {"ok": True, "status": "ok",
                    "message": "扫码登录成功，Cookie 已保存"}
        return {"ok": True, "status": r.get("status")}

    def api_push_path(self, path: str, remote_cid: int = 0) -> Dict[str, Any]:
        """手动秒传：扫描本地目录/文件并批量秒传。"""
        if not path:
            return {"ok": False, "message": "未提供本地路径"}
        target = Path(path)
        if not target.exists():
            return {"ok": False, "message": "本地路径不存在"}
        remote_cid = int(remote_cid or 0)
        # 放进后台线程，避免阻塞 API 请求
        def _bg():
            try:
                self._run_sync({"path": str(target), "remote_cid": remote_cid})
            except Exception as exc:  # noqa: BLE001
                logger.error(f"{LOG_TAG}手动秒传失败: {exc}")
        threading.Thread(target=_bg, daemon=True,
                         name=f"{LOG_TAG}push").start()
        return {"ok": True, "message": "已开始秒传，稍后查看状态"}

    def api_retry_now(self) -> Dict[str, Any]:
        """立即重试失败队列。"""
        def _bg():
            self.tick_retry(force=True)
        threading.Thread(target=_bg, daemon=True,
                         name=f"{LOG_TAG}retry").start()
        return {"ok": True, "message": "已开始重试失败项"}

    def api_status(self) -> Dict[str, Any]:
        """当前状态、重试队列、历史摘要。"""
        return {
            "ok": True,
            "enabled": self._enabled,
            "logged": bool(self._service and self._service.cookie),
            "pending": len(self._pending_retries),
            "done_count": len(self._done),
            "history": self._history[-20:],
        }

    # ------------------------------------------------------------------
    # 主流程：秒传一个本地路径（目录会递归枚举媒体文件）
    # ------------------------------------------------------------------
    def _media_exts(self) -> Set[str]:
        if self._extensions.strip():
            return {e.strip().lower().lstrip(".") for e in
                    self._extensions.replace("，", ",").split(",")
                    if e.strip()}
        return _DEFAULT_MEDIA_EXTS

    def _iter_media(self, path: Path) -> List[Path]:
        """枚举 path 下的媒体文件（目录递归）。"""
        exts = self._media_exts()
        out: List[Path] = []
        if path.is_file():
            if path.suffix.lower().lstrip(".") in exts:
                out.append(path)
            return out
        for p in path.rglob("*"):
            if p.is_file() and p.suffix.lower().lstrip(".") in exts:
                out.append(p)
        return out

    def _run_sync(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        """同步批量秒传一个本地路径。返回统计。"""
        if self._running:
            return {"success": False, "message": "已有秒传任务在运行"}
        self._running = True
        stats = {"scanned": 0, "ok": 0, "reused": 0, "failed": 0, "skipped": 0}
        try:
            if not self._service or not self._service.cookie:
                logger.warning(f"{LOG_TAG}未配置 115 Cookie，跳过秒传")
                return {**stats, "success": False, "message": "未配置 Cookie"}
            remote_cid = int(spec.get("remote_cid") or 0)
            if not remote_cid:
                remote_cid = self._resolve_remote_cid()
            path = Path(spec["path"])
            files = self._iter_media(path)
            stats["scanned"] = len(files)
            for fp in files:
                key = self._key(fp, remote_cid)
                if key in self._done:
                    stats["skipped"] += 1
                    continue
                try:
                    r = self._service.rapid_upload(fp, remote_cid)
                except (P115AuthError, P115Error) as e:
                    self._enqueue_failed(key, fp, remote_cid, str(e))
                    stats["failed"] += 1
                    continue
                if r.get("ok"):
                    self._mark_done(key)
                    self._try_remove_queue(fp)
                    stats["ok"] += 1
                    stats["reused"] += bool(r.get("reuse"))
                    self._record_history(fp, "成功", r.get("message"))
                elif r.get("need_upload"):
                    # 未命中秒传：交重试，绝不真实上传
                    self._enqueue_failed(key, fp, remote_cid, r.get("message"))
                    stats["failed"] += 1
                else:
                    self._enqueue_failed(key, fp, remote_cid, r.get("message"))
                    stats["failed"] += 1
        finally:
            self._running = False
            self._persist()
        self._send_notify(f"115秒传完成：成功 {stats['ok']}/{stats['scanned']}，"
                          f"失败重试 {stats['failed']}")
        return {**stats, "success": True}

    def _key(self, fp: Path, remote_cid: int) -> str:
        """稳定键：本地路径 + 远程目录 id + 大小（避免同文件误判已处理）。"""
        try:
            size = fp.stat().st_size
        except OSError:
            size = 0
        return f"{fp}::{remote_cid}::{size}"

    def _mark_done(self, key: str) -> None:
        self._done.add(key)
        if len(self._done) > 5000:
            self._done = set(list(self._done)[-5000:])
        self._pending_retries.pop(key, None)

    def _enqueue_failed(self, key: str, fp: Path, remote_cid: int, err: str) -> None:
        now = time.time()
        item = self._pending_retries.get(key) or {
            "path": str(fp), "remote_cid": remote_cid, "attempts": 0,
        }
        item["attempts"] = int(item.get("attempts", 0)) + 1
        item["last_error"] = str(err)[:200]
        item["next_retry"] = now + self._retry_interval
        item["updated_at"] = now
        self._pending_retries[key] = item

    def _record_history(self, fp: Path, state: str, msg: str) -> None:
        self._history.insert(0, {
            "time": time.strftime("%m-%d %H:%M"),
            "file": fp.name,
            "state": state,
            "msg": str(msg)[:80],
        })
        if len(self._history) > 200:
            self._history = self._history[:200]

    # ------------------------------------------------------------------
    # 整理队列移除
    # ------------------------------------------------------------------
    def _try_remove_queue(self, fp: Path) -> None:
        """若该源文件仍在 MP 整理队列，秒传成功后从队列移除。"""
        if not self._remove_queue:
            return
        try:
            from app.chain.transfer import TransferChain as _Chain
            from app.schemas import FileItem as _FileItem
            _Chain().remove_from_queue(_FileItem(storage="local", path=str(fp)))
            logger.info(f"{LOG_TAG}秒传成功，已将 {fp} 从整理队列移除")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG}移除整理队列失败: {exc}")

    # ------------------------------------------------------------------
    # qB 下载完成监控
    # ------------------------------------------------------------------
    def tick_qb(self) -> None:
        """轮询 qBittorrent：下载完成的新种子就地秒传，成功则从 MP 队列移除。"""
        if not self._enabled or not (self._service and self._service.cookie):
            return
        services = DownloaderHelper().get_services() or {}
        remote_cid = self._resolve_remote_cid()
        for _name, si in services.items():
            d = si.instance
            if not d or d.is_inactive():
                continue
            if not str(getattr(si, "type", "") or "").lower().startswith("qbittorrent"):
                continue
            try:
                torrents, _err = d.get_torrents(status="completed")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"{LOG_TAG}获取 qB 种子失败: {exc}")
                continue
            for t in torrents or []:
                try:
                    t_hash = str(t.get("hash") or "").strip()
                    if not t_hash:
                        continue
                    paths = self._torrent_media_files(t)
                    if not paths:
                        continue
                    for fp in paths:
                        key = self._key(fp, remote_cid)
                        if key in self._done:
                            continue
                        r = self._service.rapid_upload(fp, remote_cid)
                        if r.get("ok"):
                            self._mark_done(key)
                            self._try_remove_queue(fp)
                            self._record_history(fp, "成功", "下载完成秒传")
                        else:
                            self._enqueue_failed(key, fp, remote_cid, r.get("message"))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"{LOG_TAG}处理 qB 种子失败: {exc}")
        self._persist()

    def _torrent_media_files(self, torrent: Dict[str, Any]) -> List[Path]:
        """从种子 dictionary 提取本地媒体文件路径。"""
        exts = self._media_exts()
        content = str(torrent.get("content_path") or "")
        save = str(torrent.get("save_path") or "")
        name = str(torrent.get("name") or "")
        candidate = Path(content) if content else (Path(save) / name if save else None)
        if candidate is None:
            return []
        if candidate.is_file():
            return [candidate] if candidate.suffix.lower().lstrip(".") in exts else []
        if candidate.is_dir():
            return [p for p in candidate.rglob("*")
                    if p.is_file() and p.suffix.lower().lstrip(".") in exts]
        return []

    # ------------------------------------------------------------------
    # 失败重试
    # ------------------------------------------------------------------
    def tick_retry(self, force: bool = False) -> None:
        """重试"秒传失败且尚未被整理成功"的文件。"""
        if not self._enabled or not (self._service and self._service.cookie):
            return
        if self._running:
            return
        now = time.time()
        due = [k for k, it in self._pending_retries.items()
               if force or float(it.get("next_retry", 0)) <= now]
        if not due:
            return
        self._running = True
        try:
            remote_cid_default = self._resolve_remote_cid()
            for key in due:
                item = self._pending_retries.get(key)
                if not item:
                    continue
                fp = Path(item.get("path") or "")
                if not fp.is_file():
                    self._pending_retries.pop(key, None)
                    continue
                cid = int(item.get("remote_cid") or remote_cid_default)
                try:
                    r = self._service.rapid_upload(fp, cid)
                except (P115AuthError, P115Error) as e:
                    self._enqueue_failed(key, fp, cid, str(e))
                    continue
                if r.get("ok"):
                    self._mark_done(key)
                    self._try_remove_queue(fp)
                    self._record_history(fp, "成功", "重试秒传成功")
                else:
                    self._enqueue_failed(key, fp, cid, r.get("message"))
        finally:
            self._running = False
            self._persist()

    def _resolve_remote_cid(self) -> int:
        """把配置的目标网盘路径解析为目录 id（空=根目录）。"""
        if not self._service:
            return 0
        if not self._remote_path.strip() or self._remote_path.strip() in ("/", "根目录"):
            return 0
        try:
            return self._service.ensure_dir(self._remote_path)
        except Exception:  # noqa: BLE001
            return 0

    # ------------------------------------------------------------------
    # 事件：整理完成  → 从重试/待处理中排除（整理成功的文件不再秒传）
    # ------------------------------------------------------------------
    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event = None) -> None:
        """整理成功后，若该源文件在秒传队列，标记为不再处理。"""
        if not self._enabled:
            return
        event_data = getattr(event, "event_data", None) or {}
        fileitem = event_data.get("fileitem") if isinstance(event_data, dict) else None
        path = None
        if fileitem and hasattr(fileitem, "path"):
            path = str(fileitem.path)
        elif isinstance(fileitem, dict):
            path = str(fileitem.get("path") or "")
        if path:
            # 从 pending 中移除该源文件相关的所有重试项（整理成功 → 不再秒传）
            removed = [k for k in self._pending_retries if k.split("::", 1)[0] == path
                       or str(self._pending_retries[k].get("path", "")) == path]
            for k in removed:
                self._pending_retries.pop(k, None)
            if removed:
                self._persist()

    # ------------------------------------------------------------------
    # 持久化 / 通知
    # ------------------------------------------------------------------
    def _persist(self) -> None:
        try:
            self.save_data("done", sorted(self._done))
            self.save_data("history", self._history)
            self.save_data("retry_queue", list(self._pending_retries.values()))
        except Exception:  # noqa: BLE001
            pass

    def _persist_cookie(self) -> None:
        """扫码成功后将新 Cookie 写回配置。"""
        if self._service and self._service.cookie:
            try:
                cfg = self.get_config() or {}
                cfg["cookie"] = self._service.cookie
                self.update_config(cfg)
            except Exception:  # noqa: BLE001
                pass

    def _send_notify(self, text: str) -> None:
        if not self._notify:
            return
        try:
            self.post_message(title="硬盘秒传", text=text)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # 命令与远程命令
    # ------------------------------------------------------------------
    @eventmanager.register(EventType.PluginAction)
    def on_plugin_action(self, event: Event = None) -> None:
        """处理远程命令。"""
        if not event:
            return
        event_data = event.event_data or {}
        if isinstance(event_data, dict) and event_data.get("action") == "mp115_status":
            st = self.api_status()
            text = (f"已启用: {st['enabled']}\n"
                    f"115 登录: {'是' if st['logged'] else '否'}\n"
                    f"待重试: {st['pending']}\n"
                    f"已秒传: {st['done_count']}")
            self.post_message(title="硬盘秒传", text=text)

    # ------------------------------------------------------------------
    # 配置与详情页
    # ------------------------------------------------------------------
    def get_form(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """配置页面。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol", "props": {"cols": 12, "md": 6},
                                "content": [
                                    {"component": "VSwitch", "props": {
                                        "model": "enabled", "label": "启用插件"}},
                                ],
                            },
                            {
                                "component": "VCol", "props": {"cols": 12, "md": 6},
                                "content": [
                                    {"component": "VSwitch", "props": {
                                        "model": "notify", "label": "发送通知"}},
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol", "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField", "props": {
                                            "model": "cookie",
                                            "label": "115 Cookie",
                                            "type": "password",
                                            "hint": "网页登录 115 后复制 Cookie（UID=...; CID=...; SEID=...）。仅本插件服务端保存。",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol", "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField", "props": {
                                            "model": "remote_path",
                                            "label": "目标网盘路径",
                                            "hint": "如 /秒传；留空 = 网盘根目录。",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol", "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea", "props": {
                                            "model": "watch_paths",
                                            "label": "手动秒传目录(每行一个)",
                                            "hint": "选择本地整个文件夹，其下媒体文件会被批量尝试秒传",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol", "props": {"cols": 12, "md": 6},
                                "content": [
                                    {"component": "VSwitch", "props": {
                                        "model": "qb_enabled", "label": "监控 qB 下载完成秒传"}},
                                ],
                            },
                            {
                                "component": "VCol", "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField", "props": {
                                            "model": "qb_poll_interval",
                                            "label": "qB 轮询秒数",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol", "props": {"cols": 12, "md": 6},
                                "content": [
                                    {"component": "VSwitch", "props": {
                                        "model": "retry_enabled", "label": "定时重试失败且未整理的文件"}},
                                ],
                            },
                            {
                                "component": "VCol", "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField", "props": {
                                            "model": "retry_interval",
                                            "label": "重试间隔(秒)",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol", "props": {"cols": 12},
                                "content": [
                                    {"component": "VSwitch", "props": {
                                        "model": "remove_queue",
                                        "label": "秒传成功且文件在 MP 整理队列时移除该队列项"}},
                                ],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "cookie": "",
            "remote_path": "",
            "watch_paths": "",
            "extensions": "",
            "qb_enabled": True,
            "qb_poll_interval": 15,
            "retry_enabled": True,
            "retry_interval": 600,
            "remove_queue": True,
            "notify": True,
        }

    def get_page(self) -> List[Dict[str, Any]]:
        """详情页：展示状态、重试队列和历史。"""
        st = self.api_status()
        pending_rows = [
            {"path": it.get("path"), "attempts": it.get("attempts")}
            for it in list(self._pending_retries.values())[:10]
        ]
        page = [
            {
                "component": "VAlert", "props": {
                    "type": "info", "variant": "tonal",
                    "text": f"115 登录：{'是' if st['logged'] else '否'} · "
                            f"待重试 {st['pending']} · 已秒传 {st['done_count']}",
                },
            },
        ]
        if pending_rows:
            page.append({
                "component": "VDataTable", "props": {
                    "headers": [{"title": "文件", "key": "path"},
                                {"title": "尝试", "key": "attempts"}],
                    "items": pending_rows,
                    "density": "compact",
                },
            })
        if st["history"]:
            page.append({
                "component": "VDataTable", "props": {
                    "headers": [{"title": "时间", "key": "time"},
                                {"title": "文件", "key": "file"},
                                {"title": "状态", "key": "state"},
                                {"title": "信息", "key": "msg"}],
                    "items": st["history"],
                    "density": "compact",
                },
            })
        page.append({
            "component": "VAlert", "props": {
                "type": "warning", "variant": "tonal",
                "text": "说明：秒传只做「快传」，未命中时不会把文件本体上传到网盘，仅进入重试队列。全程只读本地文件。",
            },
        })
        return page
