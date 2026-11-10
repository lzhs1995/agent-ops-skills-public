"""仅使用临时合成文件；检查失败边界和可观察行为。"""
import contextlib
import hashlib
import http.server
import json
import os
import platform
import shutil
import socket
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agentops.common import OpsError, atomic_json, credential, read_json, sha256, task_lock
from agentops.disk import CompressionCapacity, apply_plan, capacity, plan_files
from agentops.http import Transport
from agentops.netdisk import (MIB, choose_chunk, openapi_limits, run_upload,
                             source_manifest, verify_download)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.env = mock.patch.dict(os.environ, {
            "AGENT_OPS_CONFIG_DIR": str(self.root/"config"),
            "AGENT_OPS_STATE_DIR": str(self.root/"state")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def source(self, data=b"abcdefghijklmnopqrstuvwx"):
        p = self.root/"source.txt"
        p.write_bytes(data)
        return p

    def task(self, api):
        source = self.source()
        task = dict(version=1, task_id="isolated-test", status="PLANNED", backend="openapi",
                    profile="test", created_at="2026-11-01T00:00:00+00:00",
                    local_path=str(source), remote_path="/apps/test/file.txt", completed={},
                    account_fingerprint=api.account, chunk_bytes=4,
                    source=source_manifest(source, 4))
        path = self.root/"task.json"
        atomic_json(path, task, exclusive=True)
        return path


class FakeAPI:
    def __init__(self):
        self.account = "test-account"
        self.precreates, self.creates, self.sent = 0, 0, []
        self.fail_index = None
        self.received_md5 = None

    def userinfo(self):
        return dict(account_fingerprint=self.account, limits=dict(source="isolated test fixture",
            allowed_chunk_bytes=[4], max_file_bytes=1000, max_chunks=2000))

    def precreate(self, task):
        self.precreates += 1
        return dict(uploadid="test-upload-id", block_list=list(range(len(task["source"]["block_md5"]))))

    def upload_chunk(self, task, index, data):
        self.sent.append(index)
        if self.fail_index == index:
            raise OpsError("RETRYABLE_HTTP", "synthetic transient failure", retry_after=0)
        # 模拟云端 MD5 表示与源片 MD5 不同，合并必须使用回传的实际值。
        return dict(md5=hashlib.md5(b"cloud"+data).hexdigest(), request_id=index)

    def create(self, task, md5s):
        self.creates += 1
        self.received_md5 = md5s
        return dict(path=task["remote_path"], size=task["source"]["identity"]["size"], fs_id=123)


class LimitsTests(unittest.TestCase):
    def test_incident_rejected_before_network(self):
        limits = dict(source="fixture-cookie-profile", max_file_bytes=100*1024**3,
                      allowed_chunk_bytes=[8*MIB,16*MIB], max_chunks=2048)
        with self.assertRaises(OpsError) as cm:
            choose_chunk(22091005014, limits, 8*MIB)
        self.assertEqual(cm.exception.code, "CHUNK_LIMIT")
        self.assertEqual(choose_chunk(22091005014, limits), 16*MIB)

    def test_total_size_still_rejected_with_fewer_chunks(self):
        with self.assertRaises(OpsError) as cm:
            choose_chunk(22091005014, openapi_limits(2), 16*MIB)
        self.assertEqual(cm.exception.code, "FILE_TOO_LARGE")

    def test_chunk_boundaries(self):
        limits = dict(source="fixture", max_file_bytes=10**9, allowed_chunk_bytes=[4], max_chunks=2048)
        for n in (2047,2048):
            self.assertEqual(choose_chunk(n*4, limits), 4)
        with self.assertRaises(OpsError):
            choose_chunk(2049*4, limits)

    def test_unknown_limits_and_tier_refused(self):
        with self.assertRaises(OpsError):
            choose_chunk(10, {})
        with self.assertRaises(OpsError):
            openapi_limits(9)

    def test_normal_user_cannot_request_large_chunks(self):
        with self.assertRaises(OpsError):
            choose_chunk(20*MIB, openapi_limits(0), 16*MIB)


class UploadTests(Fixture):
    def test_uploadid_durable_before_first_chunk(self):
        api = FakeAPI()
        task_path = self.task(api)
        original = api.upload_chunk
        def send(task, index, data):
            saved = read_json(task_path)
            self.assertEqual(saved["uploadid"], "test-upload-id")
            return original(task, index, data)
        api.upload_chunk = send
        self.assertEqual(run_upload(task_path, api, workers=1)["status"], "UPLOADED")
        self.assertEqual(api.precreates, 1)
        self.assertEqual(api.creates, 1)
        self.assertEqual(len(api.received_md5), 6)
        self.assertNotEqual(api.received_md5, read_json(task_path)["source"]["block_md5"])

    def test_resume_skips_confirmed_chunks_and_keeps_uploadid(self):
        api = FakeAPI()
        task_path = self.task(api)
        api.fail_index = 1
        result = run_upload(task_path, api, workers=1, retry_wait=lambda _:None)
        self.assertEqual(result["status"], "PAUSED")
        self.assertEqual(api.sent, [0,1,1,1])
        self.assertEqual(api.precreates, 1)
        api.fail_index = None
        api.sent.clear()
        self.assertEqual(run_upload(task_path, api, workers=1)["status"], "UPLOADED")
        self.assertNotIn(0, api.sent)
        self.assertEqual(api.precreates, 1)
        self.assertEqual(api.creates, 1)

    def test_finished_task_never_replayed(self):
        api = FakeAPI()
        task_path = self.task(api)
        run_upload(task_path, api)
        calls = len(api.sent)
        self.assertFalse(run_upload(task_path, api)["replayed"])
        self.assertEqual(len(api.sent), calls)
        self.assertEqual(api.creates, 1)

    def test_unknown_precreate_and_create_never_replayed(self):
        for status in ("PRECREATE_INTENT","CREATE_INTENT"):
            with self.subTest(status=status):
                api = FakeAPI()
                p = self.root/("task-"+status+".json")
                source = self.root/(status+".txt")
                source.write_text("test")
                task = dict(backend="openapi", status=status, account_fingerprint=api.account)
                atomic_json(p, task)
                with self.assertRaises(OpsError) as cm:
                    run_upload(p, api)
                self.assertEqual(cm.exception.code, "OUTCOME_UNKNOWN")
                self.assertEqual(api.precreates, 0)
                self.assertEqual(api.creates, 0)

    def test_changed_source_refused_before_precreate(self):
        api = FakeAPI()
        p = self.task(api)
        Path(read_json(p)["local_path"]).write_text("changed")
        with self.assertRaises(OpsError) as cm:
            run_upload(p, api)
        self.assertEqual(cm.exception.code, "SOURCE_CHANGED")
        self.assertEqual(api.precreates, 0)

    def test_changed_account_refused(self):
        api = FakeAPI()
        p = self.task(api)
        api.account = "another-account"
        with self.assertRaises(OpsError) as cm:
            run_upload(p, api)
        self.assertEqual(cm.exception.code, "ACCOUNT_CHANGED")

    def test_standalone_ack_survives_missing_summary_update(self):
        api = FakeAPI()
        p = self.task(api)
        api.fail_index = 1
        run_upload(p, api, workers=1, retry_wait=lambda _:None)
        task = read_json(p)
        task["completed"] = {}
        atomic_json(p, task)
        api.fail_index = None
        api.sent.clear()
        self.assertEqual(run_upload(p, api, workers=1)["status"], "UPLOADED")
        self.assertNotIn(0, api.sent)

    def test_corrupted_ack_never_used(self):
        api = FakeAPI()
        p = self.task(api)
        api.fail_index = 1
        run_upload(p, api, workers=1, retry_wait=lambda _:None)
        ack = next(self.root.glob("*-chunks/*.json"))
        d = read_json(ack)
        d["uploadid"] = "wrong-task"
        atomic_json(ack,d)
        with self.assertRaises(OpsError) as cm:
            run_upload(p,api)
        self.assertEqual(cm.exception.code,"ACK_BINDING")

    def test_independent_roundtrip_and_mismatch(self):
        api = FakeAPI()
        p = self.task(api)
        run_upload(p, api)
        source = Path(read_json(p)["local_path"])
        with self.assertRaises(OpsError) as cm:
            verify_download(p,source)
        self.assertEqual(cm.exception.code,"NOT_INDEPENDENT")
        copy = self.root/"download.txt"
        copy.write_text("wrong")
        with self.assertRaises(OpsError):
            verify_download(p,copy)
        shutil.copyfile(source,copy)
        with self.assertRaises(OpsError) as cm:
            verify_download(p,copy)
        self.assertEqual(cm.exception.code,"DOWNLOAD_RECEIPT_REQUIRED")
        observation=self.root/"synthetic-cloud-observation.txt"
        observation.write_text("Synthetic external download fixture, not a live cloud test.")
        task=read_json(p)
        evidence=self.root/"remote-evidence.json"
        atomic_json(evidence,dict(task_id=task["task_id"],remote_path=task["remote_path"],
                    size=copy.stat().st_size,downloaded_path=str(copy),observed_at="synthetic-test",
                    observation_paths=[dict(path=str(observation),sha256=sha256(observation))]))
        self.assertEqual(verify_download(p,copy,evidence)["status"],"VERIFIED")
        self.assertTrue(source.exists())

    def test_task_lock_rejects_second_consumer(self):
        p=self.root/"task.lock"
        with task_lock(p):
            with self.assertRaises(OpsError):
                with task_lock(p):
                    pass


class DiskTests(Fixture):
    def profile(self):
        return dict(cache_roots=[str(self.root)], protected_paths=[str(self.root/"keep")],
                    compression_exclusions=[str(self.root/"keep")])

    def aged(self,p,data=b"synthetic cache"):
        p.parent.mkdir(parents=True,exist_ok=True)
        p.write_bytes(data)
        old=time.time()-8*86400
        os.utime(p,(old,old))
        return p

    def test_protected_recent_archives_and_bounds(self):
        self.aged(self.root/"old.cache")
        self.aged(self.root/"keep"/"research.txt")
        self.aged(self.root/"archive.zip")
        (self.root/"recent").write_text("keep")
        plan=plan_files(self.profile(),"cache",[self.root])
        self.assertEqual([Path(r["path"]).name for r in plan["files"]],["old.cache"])
        self.assertEqual(plan["skipped"]["protected"],1)
        self.assertIsNone(plan["target_bytes"])

    def test_unknown_cache_root_refused(self):
        with self.assertRaises(OpsError):
            plan_files(self.profile(),"cache",[self.root.parent])

    def test_scan_partial_is_explicit(self):
        for i in range(5):
            self.aged(self.root/f"{i}.cache")
        plan=plan_files(self.profile(),"cache",[self.root],max_entries=2)
        self.assertTrue(plan["scan_partial"])
        self.assertEqual(len(plan["files"]),2)

    @unittest.skipUnless(platform.system() in ("Darwin","Windows"),"native platform fixture")
    def test_apply_only_fixed_cache_then_refuse_replay(self):
        p=self.aged(self.root/"old.cache")
        profile=self.profile()
        plan=plan_files(profile,"cache",[self.root])
        file=self.root/"PLAN.json"
        atomic_json(file,plan)
        result=apply_plan(file,profile,"Isolated synthetic cache test only")
        self.assertEqual(result["status"],"FINITE_SCOPE_COMPLETED")
        self.assertFalse(p.exists())
        with self.assertRaises(OpsError) as cm:
            apply_plan(file,profile,"same fixture")
        self.assertEqual(cm.exception.code,"ATTEMPT_EXISTS")

    @unittest.skipUnless(platform.system() in ("Darwin","Windows"),"native platform fixture")
    def test_open_cache_is_preserved(self):
        p=self.aged(self.root/"open.cache")
        profile=self.profile()
        plan=plan_files(profile,"cache",[self.root])
        file=self.root/"PLAN.json"
        atomic_json(file,plan)
        with p.open("rb"):
            result=apply_plan(file,profile,"Synthetic open-file test only")
        self.assertTrue(p.exists())
        receipt=read_json(read_json(result["summary"])["receipts"][0]["path"])
        self.assertEqual(receipt["state"],"SKIPPED")

    @unittest.skipUnless(platform.system()=="Darwin" and shutil.which("afsctool"),"APFS native fixture")
    def test_apfs_keeps_complete_bytes_and_identity(self):
        p=self.aged(self.root/"closed.txt",b"verified cold text\n"*60000)
        profile=self.profile()
        plan=plan_files(profile,"apfs",[self.root])
        self.assertEqual(len(plan["files"]),1)
        file=self.root/"PLAN.json"
        atomic_json(file,plan)
        before=p.stat()
        digest=sha256(p)
        result=apply_plan(file,profile,"Synthetic APFS fixture only")
        self.assertEqual(result["status"],"FINITE_SCOPE_COMPLETED")
        self.assertEqual(sha256(p),digest)
        self.assertEqual(p.stat().st_ino,before.st_ino)
        self.assertEqual(p.stat().st_mtime_ns,before.st_mtime_ns)
        self.assertTrue(p.stat().st_flags & stat.UF_COMPRESSED)
        self.assertLess(p.stat().st_blocks,before.st_blocks)
        receipt=read_json(read_json(result["summary"])["receipts"][0]["path"])
        self.assertEqual(receipt["state"],"COMPRESSED_VERIFIED")
        self.assertFalse(list(self.root.rglob("original.apfs-clone")))

    @unittest.skipUnless(platform.system()=="Darwin" and shutil.which("afsctool"),"APFS native fixture")
    def test_open_apfs_source_is_preserved_before_native_call(self):
        p=self.aged(self.root/"open.txt",b"synthetic open-file protection\n"*50000)
        profile=self.profile()
        plan=plan_files(profile,"apfs",[self.root])
        file=self.root/"PLAN.json"
        atomic_json(file,plan)
        before=p.stat()
        digest=sha256(p)
        with p.open("rb"):
            result=apply_plan(file,profile,"Synthetic APFS open-source fixture only")
        receipt=read_json(read_json(result["summary"])["receipts"][0]["path"])
        self.assertEqual(receipt["state"],"SKIPPED")
        self.assertEqual(receipt["code"],"OPEN_OR_LOCKED")
        self.assertEqual(sha256(p),digest)
        self.assertEqual(p.stat().st_flags,before.st_flags)
        self.assertFalse(list(self.root.rglob("NATIVE_INTENT.json")))

    def test_capacity_reports_actual_bytes(self):
        result=capacity(self.root)
        self.assertGreater(result["available_bytes"],0)
        if platform.system()=="Darwin":
            self.assertEqual(result["available_bytes"],
                             min(result["statvfs_available_bytes"],result["apfs_container_free_bytes"]))

    def test_compression_observation_refreshes_stale_headroom(self):
        clock=[0.0]
        healthy={"available_bytes":40*1024**3}
        low={"available_bytes":7*1024**3}
        with mock.patch("agentops.disk.time.monotonic",side_effect=lambda:clock[0]), \
                mock.patch("agentops.disk.capacity",side_effect=[healthy,low]) as measure:
            observe=CompressionCapacity(self.root)
            self.assertEqual(observe(),healthy)
            clock[0]=0.5
            self.assertEqual(observe(),healthy)
            clock[0]=1.0
            self.assertEqual(observe(),low)
            self.assertEqual(measure.call_count,2)

    def test_compression_forced_refresh_does_not_hide_capacity_failure(self):
        with mock.patch("agentops.disk.capacity",side_effect=[{"available_bytes":40*1024**3},
                OpsError("CAPACITY_UNAVAILABLE","synthetic capacity failure")]):
            observe=CompressionCapacity(self.root)
            observe()
            with self.assertRaises(OpsError) as failure:
                observe(force=True)
            self.assertEqual(failure.exception.code,"CAPACITY_UNAVAILABLE")


class TransportTests(unittest.TestCase):
    def test_profile_proxy_routes_connect_without_changing_environment(self):
        from agentops.netdisk import configured_transport
        connection=mock.MagicMock()
        response=connection.getresponse.return_value
        response.status=200
        response.getheaders.return_value=[]
        response.read.return_value=b'{}'
        before=dict(os.environ)
        with mock.patch("agentops.http.http.client.HTTPSConnection",return_value=connection) as factory:
            transport=configured_transport({"network":{"http_proxy":"http://127.0.0.1:7897"}})
            result=transport.request("GET","https://pan.baidu.com/api/probe")
        self.assertEqual(result.status,200)
        factory.assert_called_once_with("127.0.0.1",7897,timeout=10)
        connection.set_tunnel.assert_called_once_with("pan.baidu.com",443,headers={})
        self.assertEqual(dict(os.environ),before)

    def test_stalling_response_has_wall_deadline(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args):
                pass
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length","1000")
                self.end_headers()
                for _ in range(20):
                    try:
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        time.sleep(0.08)
                    except (OSError,ConnectionError):
                        break
        server=http.server.ThreadingHTTPServer(("127.0.0.1",0),Handler)
        server.daemon_threads=True
        t=threading.Thread(target=server.serve_forever,daemon=True)
        t.start()
        start=time.monotonic()
        try:
            transport=Transport(connect_timeout=0.2,read_timeout=0.3,wall_timeout=0.45,
                                allow_local_test=True,use_proxy=False)
            with self.assertRaises(OpsError):
                transport.request("GET",f"http://127.0.0.1:{server.server_port}/")
            self.assertLess(time.monotonic()-start,1.4)
        finally:
            server.shutdown()
            server.server_close()

    def test_remote_plain_http_refused(self):
        with self.assertRaises(OpsError) as cm:
            Transport().request("GET","http://example.com/")
        self.assertEqual(cm.exception.code,"HTTPS_REQUIRED")


if __name__=="__main__":
    unittest.main()

