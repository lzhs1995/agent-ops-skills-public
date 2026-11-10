"""百度传输：任务绑定、分片准入、原 uploadid 续传及独立回下载验真。"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import uuid
from pathlib import Path, PurePosixPath

from .common import (OpsError, atomic_json, cli_run, credential, identity, load_profile,
                     now, read_json, require_regular, secure_dir, sha256, state_dir,
                     task_lock, unchanged)
from .http import Transport, cloud_url, retry_after
from . import runtime
from .webclient import WebClient

MIB = 1024 ** 2
GIB = 1024 ** 3
PAN = "https://pan.baidu.com"
LIMITS_SOURCE = "https://pan.baidu.com/union/doc/基础网盘服务/上传/分片上传/"


def openapi_limits(vip_type):
    # 文档 GB/MB 采用更保守的十进制文件上限；分片采用标准 4MiB 倍数。
    tier = int(vip_type)
    if tier not in (0, 1, 2):
        raise OpsError("ACCOUNT_TIER_UNKNOWN", "未识别会员身份，不能推算上传上限。")
    return dict(backend="openapi", tier=tier, max_file_bytes=(4, 10, 20)[tier] * 10**9,
                allowed_chunk_bytes=[4*MIB] if tier == 0 else
                ([4*MIB, 8*MIB, 16*MIB] if tier == 1 else [4*MIB, 8*MIB, 16*MIB, 32*MIB]),
                # 本地保守准入预算，不宣称这是百度所有接口的官方分片上限。
                max_chunks=2000, chunk_limit_kind="local_conservative_admission_budget",
                source=LIMITS_SOURCE, checked_at=now())


def choose_chunk(size, limits, requested=None):
    if not limits or not limits.get("source"):
        raise OpsError("LIMITS_UNKNOWN", "未验证当前接口的上限；使用已登录原生客户端。")
    if size <= 0:
        raise OpsError("EMPTY_FILE", "此分片入口不上传空文件。")
    if size > limits["max_file_bytes"]:
        raise OpsError("FILE_TOO_LARGE", "文件总大小超过当前接口/账号限制，不能仅靠调大分片解决。",
                       size_bytes=size, max_file_bytes=limits["max_file_bytes"])
    candidates = [requested] if requested is not None else sorted(limits["allowed_chunk_bytes"])
    for chunk in candidates:
        if chunk in limits["allowed_chunk_bytes"] and (size+chunk-1)//chunk <= limits["max_chunks"]:
            return chunk
    raise OpsError("CHUNK_LIMIT", "分片大小或数量不满足准入条件；尚未创建上传任务。",
                   size_bytes=size, max_chunks=limits["max_chunks"])


def source_manifest(path, chunk_size):
    pin = identity(path)
    blocks, full = [], hashlib.sha256()
    with Path(path).open("rb") as f:
        while data := f.read(chunk_size):
            full.update(data)
            blocks.append(hashlib.md5(data).hexdigest())
    if not unchanged(path, pin):
        raise OpsError("SOURCE_CHANGED", "计算摘要时源文件发生变化。")
    return dict(identity=pin, sha256=full.hexdigest(), block_md5=blocks)


class OpenAPI:
    def __init__(self, token, transport=None):
        self.token = token
        self.http = transport or Transport()
        self.account = None
        self._host_lock = threading.Lock()
        self._host_cache = {}

    def call(self, endpoint, method, params=None, data=None, base=PAN):
        query = dict(params or {}, method=method, access_token=self.token)
        url = base + endpoint + "?" + urllib.parse.urlencode(query)
        headers = {"User-Agent": "pan.baidu.com"}
        if data is not None:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = None
        r = self.http.request("POST" if data is not None else "GET", url, headers, body)
        if r.status == 429 or r.status >= 500:
            raise OpsError("RETRYABLE_HTTP", "百度接口暂时没有有效响应。",
                           http_status=r.status, retry_after=retry_after(r.headers))
        if r.status != 200:
            raise OpsError("HTTP_REJECTED", "百度接口拒绝请求。", http_status=r.status)
        result = r.json()
        error = result.get("errno", result.get("error_code", 0))
        if error:
            code = "AUTH_EXPIRED" if error in (110, 111, 20016, 31023) else "BAIDU_ERROR"
            raise OpsError(code, "百度接口业务检查未通过。", errno=error)
        return result

    def userinfo(self):
        d = self.call("/rest/2.0/xpan/nas", "uinfo")
        uid = d.get("uk", d.get("baidu_name"))
        if uid is None or "vip_type" not in d:
            raise OpsError("ACCOUNT_UNKNOWN", "接口未返回可绑定的账号和会员身份。")
        self.account = hashlib.sha256(str(uid).encode()).hexdigest()
        return dict(account_fingerprint=self.account, limits=openapi_limits(d["vip_type"]))

    def precreate(self, task):
        return self.call("/rest/2.0/xpan/file", "precreate", data={
            "path": task["remote_path"], "size": task["source"]["identity"]["size"],
            "isdir": 0, "autoinit": 1, "rtype": 1,
            "block_list": json.dumps(task["source"]["block_md5"])})

    def locate(self, task):
        key = (task["uploadid"], task["remote_path"])
        with self._host_lock:
            cached = self._host_cache.get(key)
            if cached and cached[1] > time.monotonic():
                return cached[0]
            d = self.call("/rest/2.0/pcs/file", "locateupload", base="https://d.pcs.baidu.com",
                          params={"appid": 250528, "path": task["remote_path"],
                                  "uploadid": task["uploadid"], "upload_version": "2.0"})
            choices = [s.get("server") for s in d.get("servers", [])]
            if d.get("host"):
                choices.append("https://" + d["host"])
            for url in choices:
                if url and url.startswith("https://"):
                    host = cloud_url(url).rstrip("/")
                    self._host_cache[key] = (host, time.monotonic()+max(0, min(d.get("expire", 0)-5, 60)))
                    return host
            raise OpsError("UPLOAD_HOST_MISSING", "没有取得服务端当前上传域名，保留原 uploadid。")

    def upload_chunk(self, task, index, data):
        host = self.locate(task)
        query = urllib.parse.urlencode(dict(method="upload", access_token=self.token,
                    type="tmpfile", path=task["remote_path"], uploadid=task["uploadid"], partseq=index))
        boundary = "AgentOps" + uuid.uuid4().hex
        prefix = ('--' + boundary + '\r\nContent-Disposition: form-data; name="file"; filename="part"\r\n'
                  'Content-Type: application/octet-stream\r\n\r\n').encode()
        body = prefix + data + ("\r\n--" + boundary + "--\r\n").encode()
        r = self.http.request("POST", host + "/rest/2.0/pcs/superfile2?" + query,
                             {"Content-Type": "multipart/form-data; boundary=" + boundary}, body)
        if r.status == 429 or r.status >= 500:
            raise OpsError("RETRYABLE_HTTP", "分片请求暂未成功。",
                           http_status=r.status, retry_after=retry_after(r.headers))
        if r.status != 200:
            raise OpsError("HTTP_REJECTED", "分片请求被拒绝。", http_status=r.status)
        d = r.json()
        if d.get("errno", d.get("error_code", 0)):
            raise OpsError("BAIDU_ERROR", "分片接口业务检查未通过。",
                           errno=d.get("errno", d.get("error_code")))
        if not re.fullmatch("[a-fA-F0-9]{32}", d.get("md5", "")):
            raise OpsError("CHUNK_RECEIPT_MISSING", "分片响应没有有效云端摘要，不能计入已完成。")
        return dict(md5=d["md5"].lower(), request_id=d.get("request_id"))

    def create(self, task, md5s):
        return self.call("/rest/2.0/xpan/file", "create", data={
            "path": task["remote_path"], "size": task["source"]["identity"]["size"],
            "isdir": 0, "rtype": 1, "uploadid": task["uploadid"], "block_list": json.dumps(md5s)})

    def filemeta(self, fs_id):
        d = self.call("/rest/2.0/xpan/multimedia", "filemetas",
                      params={"fsids": json.dumps([int(fs_id)]), "dlink": 1})
        matches = [v for v in d.get("list", []) if int(v.get("fs_id", 0)) == int(fs_id)]
        if len(matches) != 1:
            raise OpsError("REMOTE_NOT_FOUND", "没有取得精确匹配的远端文件。")
        return matches[0]


def desktop_status(profile):
    app = Path("/Applications/BaiduNetdisk.app")
    root = Path(os.path.expandvars(profile.get("client_root", ""))).expanduser()
    return dict(backend="desktop", installed=app.exists() if os.name != "nt" else bool(profile.get("client_root")),
                client_root_present=root.is_dir() if str(root) != "." else False,
                login_verified=False, next_action="用新鲜原生窗口确认当前账号和目录；CDP 不作为必要条件。")


def configured_transport(profile):
    settings = profile.get("network", {})
    if not isinstance(settings, dict):
        raise OpsError("NETWORK_CONFIG", "本机 network 配置必须是对象。")
    return Transport(proxy_url=settings.get("http_proxy"))


def doctor(profile, network=False):
    result = dict(at=now(), desktop=desktop_status(profile), credentials_readable=False,
                  api_connection_verified=False)
    ref = profile.get("credential_ref")
    if ref:
        try:
            token = credential(ref)
            result["credentials_readable"] = True
            if network:
                account = OpenAPI(token, configured_transport(profile)).userinfo()
                result.update(api_connection_verified=True, **account)
        except OpsError as e:
            result["api_issue"] = e.as_dict()
        except (OSError, ValueError, KeyError) as e:
            result["api_issue"] = dict(code="CREDENTIAL_UNAVAILABLE", exception=type(e).__name__)
    result["recommended_backend"] = "openapi" if result["api_connection_verified"] else "desktop"
    result["web_connection_verified"]=False
    if profile.get("web_credential_ref"):
        try:
            cookie=credential(profile["web_credential_ref"])
            result["client_credentials_readable"]=True
            if network:
                account=WebClient(cookie, configured_transport(profile)).userinfo()
                result.update(web_connection_verified=True,web_account=account)
                if not result["api_connection_verified"]:
                    result["recommended_backend"]="web"
        except Exception as e:
            result["web_issue"]=e.as_dict() if isinstance(e,OpsError) else dict(code="CLIENT_LOGIN_UNAVAILABLE",exception=type(e).__name__)
    return result


def make_plan(path, remote_path, profile, profile_name, backend="auto", chunk=None):
    path = require_regular(path)
    if not remote_path.startswith("/") or ".." in PurePosixPath(remote_path).parts:
        raise OpsError("REMOTE_PATH", "必须给出精确的网盘绝对文件路径。")
    if backend == "auto":
        check = doctor(profile, network=True)
        backend = check["recommended_backend"]
        if backend=="web" and path.stat().st_size>check["web_account"]["limits"]["max_file_bytes"]:
            backend="desktop"
    else:
        check = {}
    task = dict(version=1, task_id=uuid.uuid4().hex, created_at=now(), status="PLANNED",
                backend=backend, profile=profile_name, local_path=str(path),
                remote_path=remote_path, source_deleted=False, completed={}, runtime=runtime.pin("netdisk"))
    if backend in ("openapi","web"):
        if backend=="openapi" and (not remote_path.startswith("/apps/") or len(PurePosixPath(remote_path).parts) < 4):
            raise OpsError("OPENAPI_APP_PATH", "开放平台上传需要当前应用的 /apps/应用名/文件 路径。")
        api = WebClient(credential(profile["web_credential_ref"]), configured_transport(profile)) if backend=="web" else OpenAPI(credential(profile["credential_ref"]), configured_transport(profile))
        account = api.userinfo()
        size = path.stat().st_size
        chunk = choose_chunk(size, account["limits"], chunk)
        task.update(account_fingerprint=account["account_fingerprint"], limits=account["limits"],
                    chunk_bytes=chunk, source=source_manifest(path, chunk))
    elif backend == "desktop":
        task.update(source=source_manifest(path, 4*MIB),
                    status="NATIVE_ACTION_REQUIRED",
                    instructions="沿现有原生客户端队列上传精确文件；观察远端路径/大小后独立下载到新文件，再调用 verify。")
    else:
        raise OpsError("BACKEND_UNVERIFIED", "未验证的 Cookie 脚本不接大文件；使用原生客户端或已验证开放平台。")
    return task


def check_source(task):
    p = require_regular(task["local_path"])
    if not unchanged(p, task["source"]["identity"]) or sha256(p) != task["source"]["sha256"]:
        raise OpsError("SOURCE_CHANGED", "源文件与原任务不一致；保留旧尝试，不能继续混用分片。")


def run_upload(task_path, api, *, workers=4, no_progress=180, retry_wait=None):
    task_path = Path(task_path)
    if not 1 <= workers <= 8:
        raise OpsError("WORKERS_RANGE", "并发必须在1到8之间，默认4。")
    sleep = retry_wait or time.sleep
    initial_task = read_json(task_path)
    owner = initial_task.get("account_fingerprint", "desktop")
    with task_lock(state_dir()/"netdisk"/("account-"+owner+".lock")), task_lock(task_path.with_suffix(".lock")):
        task = read_json(task_path)
        runtime.check(task, "netdisk")
        if task["backend"] not in ("openapi","web"):
            return dict(status="NATIVE_ACTION_REQUIRED", task=str(task_path),
                        instructions=task["instructions"], source_deleted=False)
        if task["status"] in ("UPLOADED", "VERIFIED"):
            return dict(status=task["status"], task=str(task_path), replayed=False)
        if task["status"] in ("PRECREATE_INTENT", "CREATE_INTENT"):
            raise OpsError("OUTCOME_UNKNOWN", "上次控制请求的结果未知；先核远端，不能盲目重建任务或合并。")
        check_source(task)
        account = api.userinfo()
        if task["account_fingerprint"] != account["account_fingerprint"]:
            raise OpsError("ACCOUNT_CHANGED", "当前账号不属于原上传任务。")
        choose_chunk(task["source"]["identity"]["size"], account["limits"], task["chunk_bytes"])
        if not task.get("uploadid"):
            task["status"] = "PRECREATE_INTENT"
            atomic_json(task_path, task)
            pre = api.precreate(task)
            if not pre.get("uploadid") or not isinstance(pre.get("block_list"), list):
                raise OpsError("PRECREATE_INCOMPLETE", "预创建未返回可恢复任务；保留原次，不自动重试。")
            needed = pre["block_list"]
            total = len(task["source"]["block_md5"])
            if len(set(needed)) != len(needed) or any(type(i) is not int or i < 0 or i >= total for i in needed):
                raise OpsError("BLOCK_LIST_INVALID", "预创建返回的分片序号不合法。")
            if pre.get("path") and pre["path"] != task["remote_path"]:
                raise OpsError("REMOTE_RENAMED", "服务端改变了目标路径，原请求保持未结算。")
            task.update(uploadid=pre["uploadid"], needed=needed, status="UPLOADING", updated_at=now())
            atomic_json(task_path, task)
        ack_dir = secure_dir(task_path.parent / (task["task_id"] + "-chunks"))
        # 每片单独原子落盘；进程在 ACK 与总状态更新之间退出也能恢复。
        for p in ack_dir.glob("*.json"):
            ack = read_json(p)
            i = ack.get("index")
            if ack.get("uploadid") != task["uploadid"] or ack.get("task_id") != task["task_id"] \
                    or i not in task["needed"] or ack.get("source_md5") != task["source"]["block_md5"][i]:
                raise OpsError("ACK_BINDING", "分片回执与原任务不匹配。")
            task["completed"][str(i)] = ack["md5"]
        stop = threading.Event()
        errors = []

        def send(index):
            with Path(task["local_path"]).open("rb") as f:
                f.seek(index * task["chunk_bytes"])
                data = f.read(task["chunk_bytes"])
            if hashlib.md5(data).hexdigest() != task["source"]["block_md5"][index]:
                raise OpsError("SOURCE_CHANGED", "待发送分片与原摘要不同。")
            for attempt in range(3):
                if stop.is_set():
                    raise OpsError("PAUSED", "本轮已暂停派发。")
                try:
                    receipt = api.upload_chunk(task, index, data)
                    ack = dict(task_id=task["task_id"], uploadid=task["uploadid"], index=index,
                               source_md5=task["source"]["block_md5"][index], at=now(), **receipt)
                    atomic_json(ack_dir / f"{index:06d}.json", ack, exclusive=True)
                    return receipt["md5"]
                except OpsError as e:
                    if e.code not in ("RETRYABLE_HTTP", "REQUEST_TIMEOUT", "NETWORK_ERROR") or attempt == 2:
                        raise
                    delay = e.details.get("retry_after", 2 ** attempt)
                    if delay>60:
                        raise OpsError("BACKOFF_REQUIRED", "服务端要求更长退避；保存原断点，稍后沿原任务继续。",retry_after=delay)
                    if retry_wait:
                        sleep(delay)
                    elif stop.wait(delay):
                        raise OpsError("PAUSED", "本轮已暂停派发。")
            raise OpsError("CHUNK_FAILED", "分片未完成。")

        pending_indices = iter(i for i in task["needed"] if str(i) not in task["completed"])
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
        active = {}
        last_ack = time.monotonic()
        try:
            def fill():
                while len(active) < workers and not stop.is_set():
                    i = next(pending_indices, None)
                    if i is None:
                        break
                    active[pool.submit(send, i)] = i
            fill()
            while active:
                done, _ = concurrent.futures.wait(active, timeout=min(1, max(no_progress/10, 0.01)),
                                                   return_when=concurrent.futures.FIRST_COMPLETED)
                if not done and not stop.is_set() and time.monotonic() - last_ack >= no_progress:
                    stop.set()
                    errors.append(dict(code="NO_PROGRESS", seconds=no_progress))
                for future in done:
                    i = active.pop(future)
                    try:
                        task["completed"][str(i)] = future.result()
                        last_ack = time.monotonic()
                    except OpsError as e:
                        stop.set()
                        errors.append(dict(index=i, **e.as_dict()))
                    except Exception as e:
                        stop.set()
                        errors.append(dict(index=i, code="WORKER_ERROR", exception=type(e).__name__))
                    task.update(updated_at=now(), status="PAUSED" if stop.is_set() else "UPLOADING")
                    atomic_json(task_path, task)
                fill()
        finally:
            stop.set()
            pool.shutdown(wait=True, cancel_futures=True)
            task["updated_at"] = now()
            if errors:
                task.update(status="PAUSED", last_errors=errors[-10:])
            atomic_json(task_path, task)
        if errors:
            return dict(status="PAUSED", task=str(task_path), completed=len(task["completed"]),
                        required=len(task["needed"]), uploadid_preserved=True, errors=errors[-3:])
        if any(str(i) not in task["completed"] for i in task["needed"]):
            raise OpsError("INCOMPLETE", "仍有分片未取得回执，不能合并。")
        check_source(task)
        md5s = [task["completed"].get(str(i), md5) for i, md5 in enumerate(task["source"]["block_md5"])]
        task["status"] = "CREATE_INTENT"
        atomic_json(task_path, task)
        created = api.create(task, md5s)
        if created.get("path") != task["remote_path"] or created.get("size") != task["source"]["identity"]["size"] \
                or not created.get("fs_id"):
            raise OpsError("REMOTE_MISMATCH", "服务端合并结果与原目标不匹配。")
        task.update(status="UPLOADED", remote={k: created[k] for k in ("path", "size", "fs_id")},
                    updated_at=now())
        atomic_json(task_path, task)
        return dict(status="UPLOADED", task=str(task_path), remote=task["remote"],
                    verification_pending=True, source_deleted=False)


def verify_download(task_path, downloaded, remote_evidence=None):
    task_path, downloaded = Path(task_path), require_regular(downloaded)
    with task_lock(task_path.with_suffix(".lock")):
        task = read_json(task_path)
        runtime.check(task, "netdisk")
        check_source(task)
        original = task["source"]
        pin = identity(downloaded)
        if (pin["dev"], pin["ino"]) == (original["identity"]["dev"], original["identity"]["ino"]):
            raise OpsError("NOT_INDEPENDENT", "下载文件不能是源文件本身或其硬链接。")
        if pin["size"] != original["identity"]["size"] or sha256(downloaded) != original["sha256"]:
            raise OpsError("HASH_MISMATCH", "独立下载副本与源文件的完整字节不一致。")
        if not unchanged(downloaded, pin):
            raise OpsError("DOWNLOAD_CHANGED", "下载文件在校验期间变化。")
        evidence = None
        if task["backend"] in ("openapi","web") and task["status"] not in ("UPLOADED", "VERIFIED"):
            raise OpsError("UPLOAD_NOT_CONFIRMED", "服务端合并尚未确认。")
        if task["backend"] == "desktop" or remote_evidence:
            if not remote_evidence:
                raise OpsError("REMOTE_EVIDENCE_REQUIRED", "原生模式需提供当前远端路径/大小及下载完成观察回执。")
            evidence_path = Path(remote_evidence)
            evidence = read_json(evidence_path)
            if evidence.get("task_id") != task["task_id"] or evidence.get("remote_path") != task["remote_path"] \
                    or evidence.get("size") != pin["size"] or evidence.get("downloaded_path") != str(downloaded) \
                    or not evidence.get("observation_paths") or not evidence.get("observed_at"):
                raise OpsError("REMOTE_EVIDENCE_MISMATCH", "原生观察没有绑定本次精确远端和下载对象。")
            for obs in evidence["observation_paths"]:
                if sha256(obs["path"]) != obs["sha256"]:
                    raise OpsError("OBSERVATION_CHANGED", "原生观察原件与记录摘要不一致。")
        elif task["status"] not in ("UPLOADED", "VERIFIED"):
            raise OpsError("UPLOAD_NOT_CONFIRMED", "服务端合并尚未确认。")
        else:
            receipt = task.get("download_receipt", {})
            p = Path(receipt.get("path", ""))
            if not p.is_file() or sha256(p) != receipt.get("sha256"):
                raise OpsError("DOWNLOAD_RECEIPT_REQUIRED", "完整字节相同不证明云回下载；请使用 download 或提供真实远端观察。")
            observed = read_json(p)
            if (observed.get("task_id"), observed.get("remote"), observed.get("downloaded_path"), observed.get("sha256")) != \
                    (task["task_id"], task["remote"], str(downloaded), original["sha256"]):
                raise OpsError("DOWNLOAD_RECEIPT_MISMATCH", "下载回执与本次精确目标不匹配。")
        integrity = None
        suffix = downloaded.suffix.lower()
        if suffix == ".zip":
            import zipfile
            with zipfile.ZipFile(downloaded) as z:
                bad = z.testzip()
            if bad:
                raise OpsError("ARCHIVE_FAILED", "ZIP 成员 CRC 校验失败。")
            integrity = "ZIP_CRC_PASS"
        elif suffix == ".7z":
            exe = shutil.which("7zz") or shutil.which("7z")
            if not exe:
                raise OpsError("ARCHIVE_TOOL_MISSING", "完整 SHA 已匹配，但还需要 7z t 验证归档。")
            p = subprocess.run([exe, "t", str(downloaded)], capture_output=True, timeout=300)
            if p.returncode:
                raise OpsError("ARCHIVE_FAILED", "7z 归档完整性检查失败。")
            integrity = "7Z_TEST_PASS"
        task.update(status="VERIFIED", verified_at=now(),
                    download=dict(path=str(downloaded), sha256=original["sha256"],
                                  identity=pin, archive_integrity=integrity),
                    remote_evidence_sha256=sha256(remote_evidence) if remote_evidence else None)
        atomic_json(task_path, task)
        return dict(status="VERIFIED", task=str(task_path), sha256=original["sha256"],
                    downloaded_path=str(downloaded), archive_integrity=integrity, source_deleted=False)


def download(task_path, destination, api):
    task_path, destination = Path(task_path), Path(destination).absolute()
    with task_lock(task_path.with_suffix(".lock")):
        task = read_json(task_path)
        runtime.check(task, "netdisk")
        if task["backend"] not in ("openapi","web") or task["status"] not in ("UPLOADED", "VERIFIED"):
            raise OpsError("NATIVE_DOWNLOAD_REQUIRED", "使用原生客户端下载到新目录，然后调用 verify。")
        if destination.exists():
            raise OpsError("DESTINATION_EXISTS", "下载目标已存在，不覆盖。", path=str(destination))
        if destination.resolve() != destination:
            raise OpsError("UNSAFE_PATH", "下载目录不经符号链接或目录联接。")
        account = api.userinfo()
        if account["account_fingerprint"] != task["account_fingerprint"]:
            raise OpsError("ACCOUNT_CHANGED", "当前账号与任务不匹配。")
        meta = api.filemeta(task["remote"]["fs_id"],task["remote_path"]) if task["backend"]=="web" else api.filemeta(task["remote"]["fs_id"])
        if meta.get("path") != task["remote_path"] or meta.get("size") != task["source"]["identity"]["size"]:
            raise OpsError("REMOTE_CHANGED", "远端文件与原合并回执不一致。")
        secure_dir(destination.parent)
        part = destination.with_name(destination.name + "." + task["task_id"] + ".partial")
        binding_path = part.with_suffix(part.suffix + ".json")
        binding = dict(task_id=task["task_id"], destination=str(destination),
                       remote=task["remote"], expected_sha256=task["source"]["sha256"])
        if part.exists():
            if not binding_path.exists() or read_json(binding_path) != binding:
                raise OpsError("PARTIAL_BINDING", "已有部分下载不属于本次任务。")
        else:
            atomic_json(binding_path, binding, exclusive=True)
            with part.open("xb"):
                pass
        offset = part.stat().st_size
        total = task["source"]["identity"]["size"]
        if offset > total:
            raise OpsError("PARTIAL_TOO_LARGE", "部分下载大小超过远端文件。")
        url = cloud_url(meta["dlink"])
        if api.token and "access_token=" not in url:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode({"access_token": api.token})
        # 分段请求使大文件下载可有界恢复；每个范围响应先完整读取再落盘。
        with part.open("ab") as f:
            while offset < total:
                end = min(offset + 4*MIB, total) - 1
                current = url
                for hop in range(5):
                    headers=api.download_headers() if hasattr(api,"download_headers") else {"User-Agent":"pan.baidu.com"}
                    headers["Range"]=f"bytes={offset}-{end}"
                    r = api.http.request("GET", current, headers, max_body=4*MIB)
                    if r.status in (301, 302, 303, 307, 308):
                        current = cloud_url(urllib.parse.urljoin(current, r.headers.get("location", "")))
                        continue
                    break
                expected_range = f"bytes {offset}-{end}/{total}"
                if r.status == 206:
                    if r.headers.get("content-range") != expected_range:
                        raise OpsError("RANGE_MISMATCH", "服务端范围响应与本地断点不匹配。")
                elif not (r.status == 200 and offset == 0 and end + 1 == total):
                    raise OpsError("RANGE_NOT_HONORED", "服务端未遵守范围请求，保留已有部分下载。")
                if len(r.body) != end - offset + 1:
                    raise OpsError("DOWNLOAD_INCOMPLETE", "本段下载字节不足，未写入该段。")
                f.write(r.body)
                f.flush()
                os.fsync(f.fileno())
                offset = end + 1
        if sha256(part) != task["source"]["sha256"]:
            raise OpsError("HASH_MISMATCH", "下载完成但完整 SHA 不匹配，保留部分文件供检查。")
        # link 不覆盖并发创建的目标；只有新目标成立后才释放本次 partial 名称。
        os.link(part, destination)
        part.unlink()
        receipt_path = task_path.parent/("DOWNLOAD-"+uuid.uuid4().hex+".json")
        atomic_json(receipt_path, dict(task_id=task["task_id"], remote=task["remote"],
                    downloaded_path=str(destination), sha256=task["source"]["sha256"],
                    observed_at=now(), origin=task["backend"]+"_range_download"), exclusive=True)
        task["download_receipt"] = dict(path=str(receipt_path), sha256=sha256(receipt_path))
        atomic_json(task_path, task)
    return verify_download(task_path, destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="baidu")
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("doctor")
    d.add_argument("--network", action="store_true")
    p = sub.add_parser("plan")
    p.add_argument("--local", required=True, type=Path)
    p.add_argument("--remote", required=True)
    p.add_argument("--backend", choices=("auto", "openapi", "web", "desktop"), default="auto")
    p.add_argument("--chunk-mib", type=int)
    p.add_argument("--task", type=Path)
    for name in ("upload", "resume"):
        c = sub.add_parser(name)
        c.add_argument("--task", required=True, type=Path)
        c.add_argument("--workers", default=4, type=int)
    v = sub.add_parser("verify")
    v.add_argument("--task", required=True, type=Path)
    v.add_argument("--downloaded", required=True, type=Path)
    v.add_argument("--remote-evidence", type=Path)
    d = sub.add_parser("download")
    d.add_argument("--task", required=True, type=Path)
    d.add_argument("--destination", required=True, type=Path)
    d = sub.add_parser("status")
    d.add_argument("--task", required=True, type=Path)
    args = parser.parse_args()

    def action():
        if args.command == "status":
            task = read_json(args.task)
            return dict(status=task["status"], task=str(args.task), backend=task["backend"],
                        confirmed_chunks=len(task.get("completed",{})), required_chunks=len(task.get("needed",[])),
                        updated_at=task.get("updated_at",task.get("created_at")),
                        runtime=task.get("runtime"), last_errors=task.get("last_errors",[])[-3:])
        if args.command == "verify":
            return verify_download(args.task, args.downloaded, args.remote_evidence)
        profile = load_profile(args.profile)
        if args.command == "doctor":
            return doctor(profile, args.network)
        if args.command == "plan":
            task = make_plan(args.local, args.remote, profile, args.profile, args.backend,
                             args.chunk_mib*MIB if args.chunk_mib else None)
            target = args.task or state_dir() / "netdisk" / task["task_id"] / "task.json"
            atomic_json(target, task, exclusive=True)
            return dict(status=task["status"], task=str(target), backend=task["backend"],
                        bytes=task["source"]["identity"]["size"], source_deleted=False,
                        chunks=len(task["source"]["block_md5"]) if task["backend"] in ("openapi","web") else None)
        task = read_json(args.task)
        if task["profile"] != args.profile:
            raise OpsError("PROFILE_MISMATCH", "指定配置与任务不同。")
        if task["backend"] == "desktop":
            return dict(status="NATIVE_ACTION_REQUIRED", task=str(args.task), instructions=task["instructions"])
        api = WebClient(credential(profile["web_credential_ref"]), configured_transport(profile)) if task["backend"]=="web" else OpenAPI(credential(profile["credential_ref"]), configured_transport(profile))
        if args.command == "download":
            return download(args.task, args.destination, api)
        return run_upload(args.task, api, workers=args.workers)
    return cli_run(action)


if __name__ == "__main__":
    raise SystemExit(main())

