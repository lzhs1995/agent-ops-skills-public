"""复用现有客户端登录的小文件通道；大文件仍交给原生客户端。"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import time
import urllib.parse
import uuid

from .common import OpsError, now
from .http import Transport, cloud_url, retry_after

MIB=1024**2


def rc4_signature(key, message):
    key=key.encode(); data=message.encode(); table=list(range(256)); j=0
    if not key:
        raise OpsError("DOWNLOAD_SIGN_MISSING","服务端没有返回下载签名材料。")
    for i in range(256):
        j=(j+table[i]+key[i%len(key)])%256
        table[i],table[j]=table[j],table[i]
    i=j=0; out=bytearray()
    for byte in data:
        i=(i+1)%256; j=(j+table[i])%256
        table[i],table[j]=table[j],table[i]
        out.append(byte^table[(table[i]+table[j])%256])
    return base64.b64encode(out).decode()


class WebClient:
    backend="web"
    token=None

    def __init__(self,cookie,transport=None):
        self.cookie=cookie
        self.http=transport or Transport()
        self.account=None
        self.login={}
        self.host=None
        self.host_until=0
        self.host_lock=threading.Lock()

    def headers(self):
        return {"Cookie":self.cookie,"Referer":"https://pan.baidu.com/disk/main",
                "Origin":"https://pan.baidu.com","User-Agent":"Mozilla/5.0"}

    def call(self,path,params=None,data=None,base="https://pan.baidu.com"):
        query={"app_id":250528,"channel":"chunlei","web":1,"clienttype":0,**(params or {})}
        if self.login.get("bdstoken"):
            query.setdefault("bdstoken",self.login["bdstoken"])
        body=urllib.parse.urlencode(data).encode() if data is not None else None
        headers=self.headers()
        if body is not None:
            headers["Content-Type"]="application/x-www-form-urlencoded"
        r=self.http.request("POST" if body is not None else "GET",base+path+"?"+urllib.parse.urlencode(query),headers,body)
        if r.status==429 or r.status>=500:
            raise OpsError("RETRYABLE_HTTP","客户端接口暂未成功。",http_status=r.status,retry_after=retry_after(r.headers))
        if r.status!=200:
            raise OpsError("HTTP_REJECTED","客户端接口拒绝请求。",http_status=r.status)
        d=r.json()
        error=d.get("errno",d.get("error_code",0))
        if error:
            raise OpsError("CLIENT_API_ERROR","客户端接口业务检查未通过。",errno=error)
        return d

    def userinfo(self):
        d=self.call("/api/gettemplatevariable",{"fields":json.dumps(["uk","bdstoken","vip_type","sign1","sign3","timestamp"])})
        self.login=d.get("result",{})
        if not self.login.get("uk") or not self.login.get("bdstoken"):
            raise OpsError("CLIENT_LOGIN_UNAVAILABLE","当前客户端登录未通过真实接口核验。")
        self.account=hashlib.sha256(str(self.login["uk"]).encode()).hexdigest()
        # vip_type=21 等属于客户端自己的编码，绝不映射成 OpenAPI 的 0/1/2。
        limits=dict(source="verified_client_small_transfer_envelope_v1",backend="web",
                    max_file_bytes=256*MIB,allowed_chunk_bytes=[4*MIB],max_chunks=64,
                    limit_kind="local_small_file_scope_not_vendor_maximum",checked_at=now())
        return dict(account_fingerprint=self.account,limits=limits,vip_type_raw=self.login.get("vip_type"))

    def meta(self,path,allow_missing=False):
        d=self.call("/api/filemetas",{"target":json.dumps([path]),"dlink":0})
        rows=d.get("info",d.get("list",[])) or []
        matches=[r for r in rows if r.get("path")==path and not r.get("isdir")]
        if len(matches)!=1:
            if allow_missing and not rows:
                return None
            raise OpsError("REMOTE_NOT_FOUND","未取得精确远端文件元数据。")
        return matches[0]

    def precreate(self,task):
        # 新上传不得覆盖已有文件；只在明确的文件不存在响应下继续。
        try:
            existing=self.meta(task["remote_path"],allow_missing=True)
        except OpsError as e:
            if e.details.get("errno") not in (12,-9,31066):
                raise
            existing=None
        if existing:
            raise OpsError("REMOTE_EXISTS","目标已有文件，不把大小相同当作本次上传成功。")
        return self.call("/api/precreate",data=dict(path=task["remote_path"],size=task["source"]["identity"]["size"],
                         isdir=0,autoinit=1,rtype=0,block_list=json.dumps(task["source"]["block_md5"])))

    def locate(self,task):
        if not self.host_lock.acquire(timeout=20):
            raise OpsError("NETWORK_ERROR","上传域名查询仍在进行，保留原任务。")
        try:
            if self.host and time.monotonic()<self.host_until:
                return self.host
            d=self.call("/rest/2.0/pcs/file",{"method":"locateupload","uploadid":task["uploadid"],
                        "path":task["remote_path"],"upload_version":"2.0"},base="https://d.pcs.baidu.com")
            choices=[r.get("server") for r in d.get("servers",[]) if isinstance(r,dict)]
            choices += [d.get("host")]
            for host in choices:
                if not host:
                    continue
                if "://" not in host:
                    host="https://"+host
                elif host.startswith("http://"):
                    host="https://"+host[7:]
                self.host=cloud_url(host).rstrip("/")
                # 缺少服务器有效期时使用短客户端缓存，不宣称知道服务端租期。
                self.host_until=time.monotonic()+max(0,min(float(d.get("expire",30)),60))
                return self.host
            raise OpsError("UPLOAD_HOST_MISSING","未取得当前客户端上传域名。")
        finally:
            self.host_lock.release()

    def upload_chunk(self,task,index,data):
        host=self.locate(task)
        query=urllib.parse.urlencode(dict(method="upload",type="tmpfile",app_id=250528,channel="chunlei",
                    web=1,clienttype=0,path=task["remote_path"],uploadid=task["uploadid"],partseq=index))
        boundary="AgentOps"+uuid.uuid4().hex
        body=('--'+boundary+'\r\nContent-Disposition: form-data; name="file"; filename="part"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()+data+('\r\n--'+boundary+'--\r\n').encode()
        headers=self.headers(); headers["Content-Type"]="multipart/form-data; boundary="+boundary
        r=self.http.request("POST",host+"/rest/2.0/pcs/superfile2?"+query,headers,body)
        if r.status==429 or r.status>=500:
            raise OpsError("RETRYABLE_HTTP","客户端分片暂未成功。",http_status=r.status,retry_after=retry_after(r.headers))
        if r.status!=200:
            raise OpsError("HTTP_REJECTED","客户端分片请求被拒绝。",http_status=r.status)
        d=r.json()
        if d.get("errno",d.get("error_code",0)) or not re.fullmatch("[0-9a-fA-F]{32}",d.get("md5","")):
            raise OpsError("CHUNK_RECEIPT_MISSING","客户端分片未取得有效回执。",errno=d.get("errno",d.get("error_code")))
        return dict(md5=d["md5"].lower(),request_id=d.get("request_id"))

    def create(self,task,md5s):
        d=self.call("/api/create",data=dict(path=task["remote_path"],size=task["source"]["identity"]["size"],isdir=0,
                  type=2,uploadid=task["uploadid"],block_list=json.dumps(md5s),
                  target_path=task["remote_path"].rsplit("/",1)[0]+"/"))
        return self.meta(task["remote_path"])

    def filemeta(self,fs_id,path):
        meta=self.meta(path)
        if int(meta.get("fs_id",0))!=int(fs_id):
            raise OpsError("REMOTE_CHANGED","客户端远端文件身份改变。")
        self.userinfo()
        sign=rc4_signature(self.login.get("sign3",""),self.login.get("sign1",""))
        d=self.call("/api/download",{"fidlist":json.dumps([int(fs_id)]),"type":"dlink","sign":sign,
                                    "timestamp":self.login.get("timestamp")})
        links=[r for r in d.get("dlink",[]) if int(r.get("fs_id",r.get("fsid",0)))==int(fs_id)]
        if len(links)!=1 or not links[0].get("dlink"):
            raise OpsError("DOWNLOAD_LINK_MISSING","客户端未返回本次文件的下载地址。")
        url=links[0]["dlink"]
        if url.startswith("http://"):
            url="https://"+url[7:]
        return dict(**meta,dlink=cloud_url(url))

    def download_headers(self):
        return {"User-Agent":"pan.baidu.com","Cookie":self.cookie}
