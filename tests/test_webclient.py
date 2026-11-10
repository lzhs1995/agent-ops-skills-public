"""客户端登录复用的只读边界与签名算法。"""
import base64
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from agentops.common import credential
from agentops.netdisk import choose_chunk, MIB
from agentops.webclient import WebClient, rc4_signature


class WebClientTests(unittest.TestCase):
    def test_rc4_matches_known_vector(self):
        result=base64.b64decode(rc4_signature("Key","Plaintext"))
        self.assertEqual(result.hex(),"bbf316e8d940af0ad3")

    def test_client_tier_is_not_openapi_tier_and_large_files_refused(self):
        api=WebClient("synthetic fixture")
        with mock.patch.object(api,"call",return_value={"result":{"uk":123,"bdstoken":"synthetic","vip_type":21}}):
            account=api.userinfo()
        self.assertEqual(account["vip_type_raw"],21)
        self.assertEqual(choose_chunk(5*MIB,account["limits"]),4*MIB)
        with self.assertRaises(Exception):
            choose_chunk(257*MIB,account["limits"])

    def test_cookie_database_read_only_and_domain_scoped(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"Cookies"
            con=sqlite3.connect(path)
            con.execute("CREATE TABLE cookies(host_key TEXT,name TEXT,value TEXT)")
            con.executemany("INSERT INTO cookies VALUES(?,?,?)",[
                (".baidu.com","BDUSS","synthetic-bduss"),
                (".baidu.com","STOKEN","synthetic-stoken"),
                ("evilbaidu.com","UNRELATED","must-not-send")])
            con.commit(); con.close()
            before=path.read_bytes()
            value=credential({"kind":"sqlite-cookie","path":str(path)})
            self.assertIn("BDUSS=synthetic-bduss",value)
            self.assertNotIn("UNRELATED",value)
            self.assertEqual(path.read_bytes(),before)


if __name__=="__main__":
    unittest.main()
