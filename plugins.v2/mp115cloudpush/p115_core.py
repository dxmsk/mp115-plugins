"""115 网盘浏览与秒传核心实现（依赖 p115client）。

- Cookie 登录 / 扫码登录并回读 Cookie
- 列出任意目录（含递归子树）
- 按 size + sha1 秒传，命中(reuse)才成功，未命中绝不把文件本体上传
- 全程只在服务端持有凭据，不写日志、不回传前端
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_QR_APP = "web"
_QR_TIMEOUT = 180


class P115Error(Exception):
    """115 网盘接口异常。"""


class P115AuthError(P115Error):
    """鉴权失败 / Cookie 失效。"""


def _load_client_module():
    """懒加载 p115client，缺失时报可读错误。

    返回: (P115Client, iterdir, iter_dirs, makedir, upload_init)
    """
    try:
        from p115client import P115Client
        from p115client.tool import iter_dirs, makedir, upload_init
        from p115client.tool.iterdir import iterdir
        return P115Client, iterdir, iter_dirs, makedir, upload_init
    except Exception as exc:  # noqa: BLE001
        raise P115Error(
            "缺少 p115client 依赖，请确认 MoviePilot 环境已安装该 Python 包"
        ) from exc


def _sha1_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """计算文件完整 SHA1（只读取，不修改文件）。"""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest().upper()


class P115Service:
    """115 网盘秒传服务：CK 登录、扫码、目录、秒传。"""

    def __init__(self, cookie: str = "", timeout: int = 30) -> None:
        self.cookie = (cookie or "").strip()
        self.timeout = timeout
        self._client: Any = None
        self._dir_cache: Dict[int, List[dict]] = {}

    # ------------------------------------------------------------------
    # 客户端
    # ------------------------------------------------------------------
    def _require_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.cookie:
            raise P115AuthError("未配置 115 Cookie")
        P115Client, _i, _m, _u = _load_client_module()
        client = P115Client(cookies=self.cookie)
        # 校验登录态
        try:
            ok = client.login_status()
        except Exception as exc:  # noqa: BLE001
            raise P115AuthError(f"115 登录校验失败: {exc}") from exc
        if not ok:
            raise P115AuthError("115 Cookie 已失效，请重新登录")
        self._client = client
        return client

    def reset(self) -> None:
        """清空缓存的客户端（Cookie 变更后调用）。"""
        self._client = None
        self._dir_cache = {}

    @property
    def user_id(self) -> Optional[int]:
        try:
            return self._require_client().user_id
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    # 扫码登录
    # ------------------------------------------------------------------
    def qr_start(self) -> Dict[str, Any]:
        """申请扫码二维码，返回用于前端展示和轮询的会话数据（不含凭据）。"""
        P115Client, _i, _m, _u = _load_client_module()
        resp = P115Client.login_qrcode_token(app=_QR_APP)
        data = resp.get("data") or {}
        if not resp.get("state") or not data.get("uid"):
            raise P115AuthError(f"获取二维码失败: {resp}")
        uid = data.get("uid")
        url = data.get("qrcode") or f"https://115.com/scan/dg-{uid}"
        return {
            "uid": uid,
            "time": data.get("time"),
            "sign": data.get("sign"),
            "expires": int(time.time()) + _QR_TIMEOUT,
            "url": url,
        }

    def qr_poll(self, sess: Dict[str, Any]) -> Dict[str, Any]:
        """
        轮询扫码状态。
        返回: {"status": waiting|scanned|ok|expired|canceled, "cookie": str|None}
        cookie 仅在 status==ok 且只在服务端保存。
        """
        P115Client, _i, _m, _u = _load_client_module()
        uid = sess.get("uid")
        sign = sess.get("sign")
        timestamp = sess.get("time")
        expires = int(sess.get("expires") or 0)
        if int(time.time()) > expires:
            return {"status": "expired"}
        if not uid or not sign or not timestamp:
            return {"status": "expired", "error": "二维码会话无效"}
        resp = P115Client.login_qrcode_scan_status(
            {"uid": uid, "time": timestamp, "sign": sign}
        )
        state = resp.get("data") or {}
        code = resp.get("code")
        st = state.get("status")
        if st == 2 or resp.get("state") is True:
            result = P115Client.login_qrcode_scan_result(uid, app=_QR_APP)
            d = result.get("data") or {}
            cookie = str(d.get("cookie") or result.get("cookie") or "")
            if not cookie:
                raise P115AuthError("扫码成功但未取得 Cookie")
            self.cookie = cookie
            self._client = None
            self._dir_cache = {}
            return {"status": "ok", "cookie": cookie}
        if st == 1:
            return {"status": "scanned"}
        if st == -1:
            return {"status": "expired"}
        if st == -2:
            return {"status": "canceled"}
        if code in (90039, 90037):
            return {"status": "scanned"}
        if code in (90038, None):
            return {"status": "waiting"}
        if code in (90033, 90034):
            return {"status": "expired"}
        if code == 90041:
            return {"status": "canceled"}
        return {"status": "waiting"}

    # ------------------------------------------------------------------
    # 目录
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize(attr: dict) -> dict:
        is_dir = bool(attr.get("is_dir"))
        return {
            "id": int(attr.get("id") or 0),
            "parent_id": int(attr.get("parent_id") or 0),
            "name": str(attr.get("name") or ""),
            "is_dir": is_dir,
            "size": int(attr.get("size") or 0),
            "sha1": str(attr.get("sha1") or ""),
            "pickcode": str(attr.get("pickcode") or ""),
            "type": str(attr.get("type") or ""),
        }

    def _list_cid_raw(self, cid: int) -> List[dict]:
        _iterdir = _load_client_module()[1]
        client = self._require_client()
        out: List[dict] = []
        for attr in _iterdir(client, cid=cid, type=99, show_dir=1, order="user_ptime", asc=1):
            out.append(self._normalize(attr))
        return out

    def list_children(self, cid: int) -> Dict[str, Any]:
        """列出某目录下的子目录与文件（目录在前）。"""
        if cid not in self._dir_cache:
            self._dir_cache[cid] = self._list_cid_raw(cid)
        items = self._dir_cache[cid]
        dirs = [i for i in items if i["is_dir"]]
        files = [i for i in items if not i["is_dir"]]
        return {"cid": cid, "children": dirs + files}

    def tree(self) -> Dict[str, Any]:
        """递归遍历整个可读目录树，只保留目录节点（iter_dirs 只返回目录）。"""
        _iter_dirs = _load_client_module()[2]
        client = self._require_client()
        root: Dict[str, Any] = {"id": 0, "name": "根目录", "children": []}
        # id -> node 索引，便于 O(1) 挂接子目录
        nodes: Dict[int, Dict[str, Any]] = {0: root}
        try:
            for item in _iter_dirs(client, cid=0, app="web"):
                item = self._normalize(item)
                if not item["is_dir"]:
                    continue
                node: Dict[str, Any] = {
                    "id": item["id"],
                    "name": item["name"],
                    "children": [],
                }
                parent = nodes.get(item["parent_id"])
                if parent is not None:
                    parent["children"].append(node)
                nodes[item["id"]] = node
        except Exception:  # noqa: BLE001
            pass  # 目录很多时遍历可能部分失败，保留已读到节点
        return {"id": 0, "name": "根目录", "children": root["children"]}

    # ------------------------------------------------------------------
    # 秒传
    # ------------------------------------------------------------------
    def rapid_upload(
        self,
        local_path: Path,
        remote_dir_id: int,
        filename: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        对单个本地文件执行秒传。
        返回: {"ok","reuse","pickcode","sha1","size","message","need_upload"}
        - ok=True 表示秒传命中成功
        - need_upload=True 表示需真实上传（本插件一律跳过）
        """
        if not local_path.is_file():
            return {"ok": False, "reuse": False, "pickcode": "",
                    "sha1": "", "size": 0, "message": "本地文件不存在",
                    "need_upload": False}
        size = local_path.stat().st_size
        sha1 = _sha1_file(local_path)
        name = filename or local_path.name
        _up = _load_client_module()[4]
        client = self._require_client()
        result = _up(
            client,
            file=str(local_path),
            pid=int(remote_dir_id),
            filesha1=sha1,
            filesize=size,
            filename=name,
        )
        if isinstance(result, dict):
            reused = bool(result.get("reuse"))
            data = result.get("data") or {}
            if reused:
                return {"ok": True, "reuse": True,
                        "pickcode": data.get("pickcode") or data.get("pick_code") or "",
                        "sha1": sha1, "size": size,
                        "message": "秒传成功", "need_upload": False}
            status = data.get("status")
            if status == 1 or status is None:
                return {"ok": False, "reuse": False, "pickcode": "",
                        "sha1": sha1, "size": size,
                        "message": "未命中秒传（需真实上传，本插件跳过）",
                        "need_upload": True}
            return {"ok": False, "reuse": False, "pickcode": "",
                    "sha1": sha1, "size": size,
                    "message": f"秒传未成功: {data}", "need_upload": status == 1}
        # 返回 multipart 上传对象 = 需要真实上传
        return {"ok": False, "reuse": False, "pickcode": "",
                "sha1": sha1, "size": size,
                "message": "未命中秒传（需真实上传，本插件跳过）",
                "need_upload": True}

    def ensure_dir(self, path: str) -> int:
        """确保远程路径存在（逐级创建/复用），返回最终目录 id。"""
        _mk = _load_client_module()[3]
        client = self._require_client()
        norm = (path or "").strip().strip("/")
        if not norm:
            return 0
        cid = 0
        for name in norm.split("/"):
            cid = _mk(client, name, pid=cid, contain_dir=False, app="web")
        return cid