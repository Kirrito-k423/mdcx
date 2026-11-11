"""Read-only qBittorrent checks. File size and playability don't prove torrent completion."""

import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import httpx

from ..config.models import DownloadGuardConfig


class DownloadNotReady(Exception):
    """Skip this file without moving it to the failure directory."""


def _key(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(path)).replace("\\", "/").rstrip("/")


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "/")


@dataclass
class DownloadRecord:
    torrent_hash: str
    name: str
    size: int


class DownloadGuard:
    READY_STATES = {"uploading", "stalledUP", "queuedUP", "pausedUP", "forcedUP", "stoppedUP"}

    def __init__(self, config: DownloadGuardConfig, *, transport=None):
        self.config = config
        self.records: dict[str, DownloadRecord] = {}
        self.blocked: dict[str, str] = {}
        self.error = "尚未完成下载状态检查"
        self.client = None
        if config.enabled:
            self.client = httpx.AsyncClient(
                base_url=config.url.rstrip("/"),
                timeout=5,
                trust_env=False,
                transport=transport,
                headers={"Referer": config.url.rstrip("/") + "/"},
            )

    async def close(self):
        if self.client is not None:
            await self.client.aclose()

    async def _get(self, endpoint: str, **params):
        response = await self.client.get("/api/v2/" + endpoint, params=params)
        if response.status_code == 403:
            username = os.environ.get(self.config.username_env, "")
            password = os.environ.get(self.config.password_env, "")
            if not username or not password:
                raise ValueError("qBittorrent 需要登录，请配置登录环境变量")
            login = await self.client.post("/api/v2/auth/login", data={"username": username, "password": password})
            if login.text.strip() != "Ok.":
                raise ValueError("qBittorrent 登录失败")
            response = await self.client.get("/api/v2/" + endpoint, params=params)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            raise ValueError("qBittorrent 返回了无效的任务数据")
        return data

    def _map(self, remote_path: str) -> Path | None:
        remote = PurePosixPath(remote_path.replace("\\", "/"))
        if ".." in remote.parts:
            return None
        for source, destination in sorted(self.config.path_mappings.items(), key=lambda item: -len(item[0])):
            try:
                relative = remote.relative_to(PurePosixPath(source))
                return Path(destination).joinpath(*relative.parts)
            except ValueError:
                continue
        return None

    def _task_reason(self, task: dict) -> str:
        state = task.get("state", "unknown")
        if state not in self.READY_STATES:
            return f"qBittorrent 任务尚不可整理（{state}）"
        if task.get("progress") != 1:
            return "qBittorrent 任务尚未下载到 100%"
        completed = task.get("completion_on", 0)
        if completed <= 0 or time.time() - completed < self.config.settle_seconds:
            return "下载刚完成，等待文件写入稳定"
        return ""

    @staticmethod
    def _file_reason(file: dict) -> str:
        if file.get("progress") != 1 or file.get("size", 0) <= 0 or file.get("priority", 0) == 0:
            return "qBittorrent 中该文件尚未完整下载或未被选中"
        return ""

    async def prepare(self, paths: list[Path]):
        if not self.config.enabled:
            return
        self.records.clear()
        self.blocked.clear()
        try:
            if not self.config.url or not self.config.path_mappings:
                raise ValueError("未配置 qBittorrent 地址和路径映射")
            tasks = await self._get("torrents/info")
            keys = {_key(path) for path in paths}
            limiter = asyncio.Semaphore(4)

            async def inspect(task):
                root = self._map(task.get("content_path") or str(PurePosixPath(task["save_path"]) / task["name"]))
                root_key = _key(root) if root is not None else ""
                if root is None or not any(_under(key, root_key) for key in keys):
                    return
                reason = self._task_reason(task)
                if reason:
                    self.blocked[_key(root)] = reason
                    return
                async with limiter:
                    files = await self._get("torrents/files", hash=task["hash"])
                for file in files:
                    local = self._map(str(PurePosixPath(task["save_path"]) / file["name"]))
                    if local is None or _key(local) not in keys:
                        continue
                    reason = self._file_reason(file)
                    if reason:
                        self.blocked[_key(local)] = reason
                    else:
                        self.records[_key(local)] = DownloadRecord(task["hash"], file["name"], file["size"])

            # Wait for all readers before deciding whether the snapshot is trustworthy.
            results = await asyncio.gather(*(inspect(task) for task in tasks), return_exceptions=True)
            if any(isinstance(result, Exception) for result in results):
                raise ValueError("无法完整读取 qBittorrent 的文件进度")
            self.error = ""
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            self.error = "无法确认 qBittorrent 下载状态，已暂停本轮整理；请检查连接、登录和路径映射"

    async def reason(self, path: Path, *, refresh: bool = False) -> str:
        if not self.config.enabled:
            return ""
        if self.error:
            return self.error
        key = _key(path)
        for root, reason in self.blocked.items():
            if _under(key, root):
                return reason
        record = self.records.get(key)
        if record is None:
            return "未找到对应的 qBittorrent 文件记录，无法确认完整性"
        try:
            if refresh:
                tasks = await self._get("torrents/info", hashes=record.torrent_hash)
                if len(tasks) != 1 or tasks[0].get("hash") != record.torrent_hash:
                    return "下载任务已经改变或被移除"
                if reason := self._task_reason(tasks[0]):
                    return reason
                files = await self._get("torrents/files", hash=record.torrent_hash)
                file = next((file for file in files if file.get("name") == record.name), None)
                if file is None or (reason := self._file_reason(file)):
                    return reason if file else "下载任务的文件清单已改变"
                local = self._map(str(PurePosixPath(tasks[0]["save_path"]) / record.name))
                if local is None or _key(local) != key or file["size"] != record.size:
                    return "下载文件的位置或大小记录已改变"
            stat = await asyncio.to_thread(path.stat)
            if stat.st_size != record.size:
                return "本地文件大小与 qBittorrent 记录不一致"
            if time.time() - stat.st_mtime < self.config.settle_seconds:
                return "文件刚被写入，等待稳定后再刮削"
            return ""
        except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError):
            return "无法重新确认下载完整性，已跳过"

    async def ensure_ready(self, path: Path):
        if reason := await self.reason(path, refresh=True):
            raise DownloadNotReady(reason)
