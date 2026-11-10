"""验证安装隔离、发布检查和真实下载回执；不使用用户文件或网络账号。"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from agentops.common import OpsError, atomic_json, read_json, sha256
from agentops.disk import apply_plan, plan_files, verify
from agentops.http import Response
from agentops.netdisk import download, run_upload, verify_download
from test_workflows import Fixture, FakeAPI

ROOT = Path(__file__).resolve().parents[1]


def load_tool(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/"tools"/(name+".py"))
    mod=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class InstallTests(unittest.TestCase):
    def test_json_cli_uses_utf8_even_with_ascii_pipe(self):
        env=dict(os.environ, PYTHONIOENCODING="ascii")
        script="from agentops.common import cli_run; raise SystemExit(cli_run(lambda: {'message': '中文路径与恢复回执'}))"
        result=subprocess.run([sys.executable,"-B","-c",script],cwd=ROOT,env=env,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout.decode("utf-8"))["message"],"中文路径与恢复回执")

    def test_frozen_release_is_reused_and_tamper_refused(self):
        tool=load_tool("install")
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp)
            release,manifest=tool.stage_release(ROOT,base)
            again,_=tool.stage_release(ROOT,base)
            self.assertEqual(release,again)
            (release/"agentops/common.py").write_text("changed")
            with self.assertRaises(OpsError):
                tool.stage_release(ROOT,base)

    def test_policy_update_preserves_existing_text_and_backup(self):
        tool=load_tool("install")
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            policy=root/"AGENTS.md"
            before="existing unrelated policy\n"
            policy.write_text(before)
            result=tool.add_block(policy,"new two-skill routing",root)
            self.assertTrue(policy.read_text().startswith(before))
            self.assertEqual(Path(result["backup"]).read_text(),before)
            tool.add_block(policy,"updated two-skill routing",root)
            self.assertEqual(policy.read_text().count(tool.BEGIN),1)
            self.assertIn("updated two-skill routing",policy.read_text())

    @unittest.skipIf(os.name=="nt","symbolic link privilege differs on Windows")
    def test_unrelated_skill_is_never_overwritten(self):
        tool=load_tool("install")
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            target=root/"skill"
            target.mkdir()
            (target/"keep").write_text("user content")
            with self.assertRaises(OpsError):
                tool.mac_link(target,root/"new",root/"releases")
            self.assertEqual((target/"keep").read_text(),"user content")

    def test_scanner_checks_staged_blob_and_history(self):
        tool=load_tool("secret_check")
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            def git(*args):
                return subprocess.run(["git","-C",str(root),*args],capture_output=True,check=True)
            git("init","-q")
            # 只产生合成 key，写入 Git 历史后又从工作树清除。
            fake="ghp_"+"A"*36
            f=root/"fixture.txt"
            f.write_text(fake)
            git("add","fixture.txt")
            f.write_text("clean working tree")
            staged=tool.check_tree(root)
            self.assertEqual(staged["status"],"FAIL")
            self.assertTrue(any(r["path"].startswith("index:") for r in staged["findings"]))
            git("-c","user.name=Fixture","-c","user.email=fixture@example.invalid","commit","-qm","fixture")
            git("add","fixture.txt")
            git("-c","user.name=Fixture","-c","user.email=fixture@example.invalid","commit","-qm","remove fixture")
            historic=tool.check_tree(root,history=True)
            self.assertEqual(historic["status"],"FAIL")
            self.assertTrue(any(r["path"].startswith("history:") for r in historic["findings"]))

    def test_wsl_launchers_have_real_newlines(self):
        tool=load_tool("cc_switch_register")
        fake=mock.Mock(returncode=0,stdout=b'{}')
        with mock.patch.object(tool.subprocess,"run",return_value=fake) as call:
            tool.wsl_entry("Ubuntu")
        script=call.call_args.args[0][-1]
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for name,command in tool.NAMES.items():
                entry=root/".agents/skills"/name/"scripts"/(command+".py")
                entry.parent.mkdir(parents=True)
                entry.write_text("pass")
                (entry.parent.parent/"installed-release.json").write_text(json.dumps({"python":"fixture-python"}))
            script=script.replace("h=pathlib.Path.home()", "h=pathlib.Path("+repr(str(root))+")")
            result=subprocess.run([sys.executable,"-B","-c",script],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            for command in tool.NAMES.values():
                text=(root/".local/bin"/("agent-"+command)).read_text()
                self.assertTrue(text.startswith("#!/bin/sh\n"))
                self.assertIn(' "$@"\n',text)

    def test_new_entry_dispatches_to_original_task_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            original=root/"original.py"
            original.write_text('print("ORIGINAL_TASK_RUNTIME")\n')
            task=root/"task.json"
            atomic_json(task,dict(runtime=dict(entry=str(original),python=sys.executable,entry_sha256=sha256(original))))
            entry=ROOT/"skills/baidu-netdisk-transfer/scripts/netdisk.py"
            result=subprocess.run([sys.executable,"-B",str(entry),"status","--task",str(task)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout.strip(),"ORIGINAL_TASK_RUNTIME")
            original.write_text('print("CHANGED")\n')
            changed=subprocess.run([sys.executable,"-B",str(entry),"status","--task",str(task)],capture_output=True,text=True)
            self.assertEqual(changed.returncode,2)
            self.assertIn("ORIGINAL_ENTRY_CHANGED",changed.stdout)


class DownloadTests(Fixture):
    def test_download_records_remote_origin_and_rejects_receipt_tamper(self):
        api=FakeAPI()
        p=self.task(api)
        run_upload(p,api,workers=1)
        task=read_json(p)
        payload=Path(task["local_path"]).read_bytes()
        api.token="synthetic-test-credential"
        api.filemeta=lambda _:dict(path=task["remote_path"],size=len(payload),dlink="https://example.baidupcs.com/file")
        class FakeHTTP:
            def request(self,method,url,headers,max_body):
                if headers["Range"] != "bytes=0-23":
                    raise AssertionError("wrong byte range")
                return Response(206,{"content-range":"bytes 0-23/24"},payload)
        api.http=FakeHTTP()
        dest=self.root/"fresh-download.txt"
        result=download(p,dest,api)
        self.assertEqual(result["status"],"VERIFIED")
        self.assertEqual(dest.read_bytes(),payload)
        receipt=read_json(p)["download_receipt"]
        Path(receipt["path"]).write_text("{}")
        with self.assertRaises(OpsError) as cm:
            verify_download(p,dest)
        self.assertEqual(cm.exception.code,"DOWNLOAD_RECEIPT_REQUIRED")

    def test_429_honors_delay(self):
        api=FakeAPI()
        p=self.task(api)
        original=api.upload_chunk
        attempts=[0]
        def transient(task,index,data):
            if index==0 and attempts[0]<2:
                attempts[0]+=1
                raise OpsError("RETRYABLE_HTTP","synthetic 429",http_status=429,retry_after=0.17)
            return original(task,index,data)
        api.upload_chunk=transient
        delays=[]
        self.assertEqual(run_upload(p,api,workers=1,retry_wait=delays.append)["status"],"UPLOADED")
        self.assertEqual(delays,[0.17,0.17])

    def test_403_is_not_retried(self):
        api=FakeAPI()
        p=self.task(api)
        calls=[]
        def denied(task,index,data):
            calls.append(index)
            raise OpsError("HTTP_REJECTED","synthetic 403",http_status=403)
        api.upload_chunk=denied
        result=run_upload(p,api,workers=1)
        self.assertEqual(result["status"],"PAUSED")
        self.assertEqual(calls,[0])
        self.assertEqual(api.creates,0)

    def test_long_server_backoff_pauses_without_retrying_early(self):
        api=FakeAPI()
        p=self.task(api)
        calls=[]
        def throttled(task,index,data):
            calls.append(index)
            raise OpsError("RETRYABLE_HTTP","synthetic long backoff",retry_after=300)
        api.upload_chunk=throttled
        delays=[]
        result=run_upload(p,api,workers=1,retry_wait=delays.append)
        self.assertEqual(result["status"],"PAUSED")
        self.assertEqual(calls,[0])
        self.assertEqual(delays,[])
        self.assertEqual(result["errors"][0]["code"],"BACKOFF_REQUIRED")

    def test_no_ack_deadline_pauses_and_preserves_original_upload(self):
        api=FakeAPI()
        p=self.task(api)
        original=api.upload_chunk
        def slow(task,index,data):
            time.sleep(0.12)
            return original(task,index,data)
        api.upload_chunk=slow
        result=run_upload(p,api,workers=1,no_progress=0.02)
        self.assertEqual(result["status"],"PAUSED")
        self.assertEqual(api.precreates,1)
        self.assertEqual(api.creates,0)
        self.assertEqual(len(api.sent),1)
        api.upload_chunk=original
        self.assertEqual(run_upload(p,api,workers=1)["status"],"UPLOADED")
        self.assertEqual(api.precreates,1)


class CapacityVerificationTests(Fixture):
    def test_two_samples_keep_target_unmet(self):
        summary=self.root/"SUMMARY.json"
        atomic_json(summary,dict(receipts=[],volume=str(self.root),target_bytes=100,status="FINITE_SCOPE_COMPLETED"))
        with mock.patch("agentops.disk.capacity",side_effect=[dict(available_bytes=101),dict(available_bytes=99)]):
            result=verify(summary,interval=0)
        self.assertFalse(result["target_met"])
        self.assertEqual(len(result["samples"]),2)
        self.assertEqual(result["minimum_available_bytes"],99)


if __name__ == "__main__":
    unittest.main()
