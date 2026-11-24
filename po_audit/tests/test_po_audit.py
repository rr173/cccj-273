"""po_audit 测试套件（stdlib unittest）。

运行：python3 -m unittest discover -s tests -v   （在 /workspace/po_audit 下）
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from po_audit import fixtures, model
from po_audit.offline import load_and_verify, IntegrityError
from po_audit.report import compute_report
from po_audit.slicer import slice_package
from po_audit.warehouse import Warehouse
from po_audit.whreport import warehouse_report

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_cli(module, *args):
    env = dict(os.environ, PYTHONPATH=PROJECT_ROOT)
    return subprocess.run(
        [sys.executable, "-m", module, *args],
        capture_output=True, text=True, env=env, cwd="/",
    )


class FixtureBase(unittest.TestCase):
    """生成一套夹具仓与交付包，供全部用例复用。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="po_audit_test_")
        cls.wh_dir = os.path.join(cls.tmp, "warehouse")
        cls.pkg_dir = os.path.join(cls.tmp, "package")
        fixtures.generate(cls.wh_dir, seed=7)
        cls.wh = Warehouse.load(cls.wh_dir)
        with open(os.path.join(cls.wh_dir, "requests.json"), encoding="utf-8") as fh:
            cls.requests = json.load(fh)
        with open(os.path.join(cls.wh_dir, "crafted.json"), encoding="utf-8") as fh:
            cls.crafted = json.load(fh)
        cls.summary = slice_package(cls.wh, cls.requests, cls.pkg_dir)
        cls.pkg = load_and_verify(cls.pkg_dir)
        with open(os.path.join(cls.pkg_dir, "manifest.json"), encoding="utf-8") as fh:
            cls.manifest = json.load(fh)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def case(self, request_id):
        for c in self.manifest["cases"]:
            if c["request_id"] == request_id:
                return c
        raise AssertionError(f"manifest 中无 {request_id}")

    def offline_report(self, request_id):
        case = self.pkg.case(request_id)
        rows = self.pkg.case_rows(case)
        rep = compute_report(case["doc_row_id"], rows["documents"], rows["items"],
                             rows["arrivals"], rows["suspensions"])
        rep["request_id"] = request_id
        rep["warehouse_version"] = self.manifest["warehouse_version"]
        return rep

    def pkg_row_ids(self, table):
        return set(self.pkg.rows[table])


class TestSchema(unittest.TestCase):
    def _row(self, **kw):
        base = {"doc_row_id": "D-1", "supplier": "S", "doc_no": "N",
                "permit": True, "restriction": None}
        base.update(kw)
        return base

    def test_valid_document(self):
        model.validate_row("documents", self._row())

    def test_negative_qty_rejected(self):
        with self.assertRaises(model.SchemaError):
            model.validate_row("arrivals", {
                "arrival_row_id": "A-1", "supplier": "S", "doc_no": "N",
                "item_no": "X", "qty": -1, "permit": True, "restriction": None})

    def test_bool_qty_rejected(self):
        with self.assertRaises(model.SchemaError):
            model.validate_row("items", {
                "item_row_id": "I-1", "supplier": "S", "doc_no": "N",
                "item_no": "X", "expected_qty": True, "permit": True, "restriction": None})

    def test_null_item_no_rejected_for_items(self):
        with self.assertRaises(model.SchemaError):
            model.validate_row("items", {
                "item_row_id": "I-1", "supplier": "S", "doc_no": "N",
                "item_no": None, "expected_qty": 1, "permit": True, "restriction": None})

    def test_empty_string_item_no_allowed(self):
        model.validate_row("items", {
            "item_row_id": "I-1", "supplier": "S", "doc_no": "N",
            "item_no": "", "expected_qty": 0, "permit": True, "restriction": None})

    def test_restricted_row_requires_category(self):
        with self.assertRaises(model.SchemaError):
            model.validate_row("documents", self._row(permit=False, restriction=None))

    def test_permitted_row_must_not_carry_category(self):
        with self.assertRaises(model.SchemaError):
            model.validate_row("documents", self._row(restriction="oops"))

    def test_duplicate_physical_id_rejected(self):
        lines = [
            json.dumps(self._row(doc_row_id="D-1")),
            json.dumps(self._row(doc_row_id="D-1")),
        ]
        with self.assertRaises(model.SchemaError):
            model.parse_table_lines(lines, "documents", "test")

    def test_unknown_field_rejected(self):
        with self.assertRaises(model.SchemaError):
            model.validate_row("documents", self._row(extra="x"))


class TestScopeAndSemantics(FixtureBase):
    def test_offline_report_matches_warehouse_reference(self):
        for req in self.requests:
            case = self.case(req["request_id"])
            if case["status"] != "delivered":
                continue
            offline = self.offline_report(req["request_id"])
            ref = warehouse_report(self.wh, req["doc_row_id"])
            ref["request_id"] = req["request_id"]
            self.assertEqual(offline, ref, req["request_id"])

    def test_duplicate_items_no_fanout(self):
        """重复项目行：一份到货对每个同项目行各贡献一次，不乘量。"""
        rep = self.offline_report("REQ-B")
        self.assertTrue(rep["duplicate_doc_number"]["is_duplicate"])
        self.assertEqual(len(rep["duplicate_doc_number"]["doc_row_ids"]), 3)
        self.assertEqual(len(rep["items"]), 2)
        for entry in rep["items"]:
            self.assertEqual(entry["arrived_qty"], 8)
            self.assertEqual(entry["fulfillment"], "exact")
        # 共享到货原行在包内只有一份
        arr_id = self.crafted["dup_items"]["arrival"]
        self.assertIn(arr_id, self.pkg_row_ids("arrivals"))
        with open(os.path.join(self.pkg_dir, "arrivals.jsonl"), encoding="utf-8") as fh:
            data = fh.read()
        self.assertEqual(data.count(arr_id), 1)

    def test_empty_doc_duplicate_and_suspended_not_masked(self):
        """重号+空单+整单挂起同时成立，互不掩盖。"""
        rep = self.offline_report("REQ-C")
        self.assertTrue(rep["duplicate_doc_number"]["is_duplicate"])
        self.assertTrue(rep["empty_document"])
        self.assertTrue(rep["document_suspended"])
        self.assertEqual(rep["items"], [])
        self.assertEqual(len(rep["suspensions"]), 1)
        self.assertEqual(rep["suspensions"][0]["scope"], "document")

    def test_empty_string_is_not_null(self):
        """空字符串项目号是合法键；'' 挂起只命中 '' 项目；无到货为 0。"""
        rep = self.offline_report("REQ-D")
        self.assertFalse(rep["document_suspended"])
        by_no = {e["item_no"]: e for e in rep["items"]}
        self.assertEqual(by_no[""]["arrived_qty"], 5)
        self.assertEqual(by_no[""]["fulfillment"], "over")
        self.assertTrue(by_no[""]["suspended"])
        self.assertEqual(by_no["X"]["arrived_qty"], 0)
        self.assertEqual(by_no["X"]["fulfillment"], "short")
        self.assertFalse(by_no["X"]["suspended"])

    def test_scope_minimality(self):
        """必要范围恰为同单键全部单据/项目、命中项目键全部到货、适用挂起。"""
        expected = {t: set() for t in model.TABLES}
        for req in self.requests:
            case = self.case(req["request_id"])
            if case["status"] != "delivered":
                continue
            scope = self.wh.compute_scope(req["request_id"], req["doc_row_id"])
            for table, row in scope.rows():
                expected[table].add(row[model.ID_FIELD[table]])
        for t in model.TABLES:
            self.assertEqual(self.pkg_row_ids(t), expected[t], t)

    def test_stray_rows_excluded(self):
        """不命中项目键的到货与不适用挂起不得入包。"""
        self.assertNotIn(self.crafted["clean"]["stray_arrival"], self.pkg_row_ids("arrivals"))
        self.assertNotIn(self.crafted["clean"]["stray_suspension"], self.pkg_row_ids("suspensions"))
        self.assertNotIn(self.crafted["empty_string"]["stray_suspension"],
                         self.pkg_row_ids("suspensions"))

    def test_shared_rows_delivered_once(self):
        """两案共享同键原行，只交一份。"""
        shared = self.crafted["shared"]
        for did in shared["doc_row_ids"]:
            self.assertIn(did, self.pkg_row_ids("documents"))
        with open(os.path.join(self.pkg_dir, "documents.jsonl"), encoding="utf-8") as fh:
            data = fh.read()
        for did in shared["doc_row_ids"]:
            self.assertEqual(data.count(did), 1)
        g1 = self.case("REQ-G1")
        g2 = self.case("REQ-G2")
        q1 = {(q["table"], json.dumps(q["key"], sort_keys=True)): set(q["row_ids"])
              for q in g1["queries"]}
        q2 = {(q["table"], json.dumps(q["key"], sort_keys=True)): set(q["row_ids"])
              for q in g2["queries"]}
        self.assertEqual(q1, q2)


class TestRefusals(FixtureBase):
    def test_refused_cases(self):
        e = self.case("REQ-E")
        f = self.case("REQ-F")
        self.assertEqual(e["status"], "refused")
        self.assertEqual(e["restricted_categories"], ["legal-hold"])
        self.assertEqual(f["status"], "refused")
        self.assertEqual(f["restricted_categories"], ["privacy"])

    def test_refusal_leaks_only_request_id_and_categories(self):
        """拒发只给请求ID及受限类别，不外露受限行身份、数量或内容。"""
        for rid in ("REQ-E", "REQ-F"):
            case = self.case(rid)
            self.assertEqual(set(case.keys()),
                             {"request_id", "status", "restricted_categories"})
        # 受限行的物理ID与内容（如挂起原因）不得出现在清单任何位置
        blob = json.dumps(self.manifest, ensure_ascii=False)
        for secret in (self.crafted["refused_arrival"]["restricted_arrival"],
                       self.crafted["refused_suspension"]["restricted_suspension"],
                       "do not disclose"):
            self.assertNotIn(secret, blob)

    def test_refused_case_rows_not_delivered(self):
        """任一必要行受限则整案拒发：该案所有必要行（含未受限行）均不入包。"""
        for rid, crafted_key in (("REQ-E", "refused_arrival"), ("REQ-F", "refused_suspension")):
            info = self.crafted[crafted_key]
            scope = self.wh.compute_scope(rid, info["doc_row_ids"][0])
            for table, row in scope.rows():
                self.assertNotIn(row[model.ID_FIELD[table]], self.pkg_row_ids(table),
                                 f"{rid}: {table}/{row}")

    def test_independent_case_still_delivered(self):
        self.assertEqual(self.case("REQ-U")["status"], "delivered")
        rep = self.offline_report("REQ-U")
        self.assertEqual(rep["items"][0]["fulfillment"], "exact")

    def test_unknown_request(self):
        self.assertEqual(self.case("REQ-UNKNOWN")["status"], "unknown_request")


class TestManifest(FixtureBase):
    def test_empty_queries_declared(self):
        """范围内确实无行的查询被显式声明。"""
        c = self.case("REQ-C")
        self.assertIn("items", c["empty_queries"])
        d = self.case("REQ-D")
        self.assertIn("arrivals[item_no='X']", d["empty_queries"])
        a = self.case("REQ-A")
        self.assertIn("suspensions", a["empty_queries"])
        # empty 标记与 row_ids 一致
        for case in self.manifest["cases"]:
            for q in case.get("queries", []):
                self.assertEqual(q["empty"], len(q["row_ids"]) == 0)

    def test_arrival_queries_cover_every_item_key(self):
        for req in self.requests:
            case = self.case(req["request_id"])
            if case["status"] != "delivered":
                continue
            items_q = [q for q in case["queries"] if q["table"] == "items"][0]
            item_nos = {self.pkg.rows["items"][i]["item_no"] for i in items_q["row_ids"]}
            arr_keys = {q["key"]["item_no"] for q in case["queries"] if q["table"] == "arrivals"}
            self.assertEqual(arr_keys, item_nos, req["request_id"])

    def test_row_hashes_cover_all_rows(self):
        for t in model.TABLES:
            self.assertEqual(set(self.manifest["row_hashes"][t]), self.pkg_row_ids(t))

    def test_refused_and_unknown_carry_no_queries(self):
        for case in self.manifest["cases"]:
            if case["status"] in ("refused", "unknown_request"):
                self.assertNotIn("queries", case)


class TestOfflineCli(FixtureBase):
    def test_verify_ok(self):
        p = run_cli("po_audit.offline", "--package", self.pkg_dir, "verify")
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_missing_file_rejected(self):
        broken = os.path.join(self.tmp, "broken_missing")
        shutil.copytree(self.pkg_dir, broken)
        os.remove(os.path.join(broken, "items.jsonl"))
        p = run_cli("po_audit.offline", "--package", broken, "verify")
        self.assertEqual(p.returncode, 10)
        self.assertIn("缺文件", p.stderr)

    def test_tampered_row_rejected(self):
        broken = os.path.join(self.tmp, "broken_tamper")
        shutil.copytree(self.pkg_dir, broken)
        path = os.path.join(broken, "arrivals.jsonl")
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        row = json.loads(lines[0])
        row["qty"] += 1
        lines[0] = json.dumps(row, ensure_ascii=False)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        p = run_cli("po_audit.offline", "--package", broken, "verify")
        self.assertEqual(p.returncode, 10)

    def test_dropped_row_rejected(self):
        """删掉一行（模拟删除证据）→ 完整性失败，绝不宣称合格。"""
        broken = os.path.join(self.tmp, "broken_dropped")
        shutil.copytree(self.pkg_dir, broken)
        path = os.path.join(broken, "arrivals.jsonl")
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines[1:]) + "\n")
        p = run_cli("po_audit.offline", "--package", broken, "verify")
        self.assertEqual(p.returncode, 10)

    def test_out_of_scope_query_rejected(self):
        p = run_cli("po_audit.offline", "--package", self.pkg_dir,
                    "report", "--request", "REQ-NOPE")
        self.assertEqual(p.returncode, 11)

    def test_refused_exit_code(self):
        p = run_cli("po_audit.offline", "--package", self.pkg_dir,
                    "report", "--request", "REQ-E")
        self.assertEqual(p.returncode, 12)
        out = json.loads(p.stdout)
        self.assertEqual(set(out.keys()), {"request_id", "status", "restricted_categories"})

    def test_unknown_request_exit_code(self):
        p = run_cli("po_audit.offline", "--package", self.pkg_dir,
                    "report", "--request", "REQ-UNKNOWN")
        self.assertEqual(p.returncode, 13)

    def test_cli_report_matches_warehouse(self):
        p = run_cli("po_audit.offline", "--package", self.pkg_dir,
                    "report", "--request", "REQ-A")
        self.assertEqual(p.returncode, 0, p.stderr)
        offline = json.loads(p.stdout)
        ref = warehouse_report(self.wh, self.crafted["clean"]["doc_row_ids"][0])
        ref["request_id"] = "REQ-A"
        self.assertEqual(offline, ref)


class TestSignature(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="po_audit_sig_")
        self.wh_dir = os.path.join(self.tmp, "wh")
        fixtures.generate(self.wh_dir, seed=11, filler_docs=5,
                          hot_docs=2, hot_items=3, hot_arrivals=5)
        self.wh = Warehouse.load(self.wh_dir)
        with open(os.path.join(self.wh_dir, "requests.json"), encoding="utf-8") as fh:
            self.requests = json.load(fh)[:3]
        self.key_file = os.path.join(self.tmp, "key")
        with open(self.key_file, "wb") as fh:
            fh.write(b"owner-secret")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_signed_manifest_roundtrip(self):
        pkg = os.path.join(self.tmp, "pkg")
        with open(self.key_file, "rb") as fh:
            key = fh.read()
        slice_package(self.wh, self.requests, pkg, hmac_key=key)
        # 正确密钥验签通过
        p = run_cli("po_audit.offline", "--package", pkg,
                    "--hmac-key-file", self.key_file, "verify")
        self.assertEqual(p.returncode, 0, p.stderr)
        # 缺密钥拒绝
        p = run_cli("po_audit.offline", "--package", pkg, "verify")
        self.assertEqual(p.returncode, 10)
        # 错密钥拒绝
        wrong = os.path.join(self.tmp, "wrong")
        with open(wrong, "wb") as fh:
            fh.write(b"wrong")
        p = run_cli("po_audit.offline", "--package", pkg,
                    "--hmac-key-file", wrong, "verify")
        self.assertEqual(p.returncode, 10)

    def test_unsigned_manifest_with_key_rejected(self):
        pkg = os.path.join(self.tmp, "pkg2")
        slice_package(self.wh, self.requests, pkg)
        p = run_cli("po_audit.offline", "--package", pkg,
                    "--hmac-key-file", self.key_file, "verify")
        self.assertEqual(p.returncode, 10)


class TestHotKeyPerformance(unittest.TestCase):
    """热点键：切片与离线检验均为索引化，代价随必要范围线性，不逐案重扫原表。"""

    def test_hot_key_slice_and_verify(self):
        tmp = tempfile.mkdtemp(prefix="po_audit_hot_")
        try:
            wh_dir = os.path.join(tmp, "wh")
            fixtures.generate(wh_dir, seed=3, filler_docs=300,
                              hot_docs=500, hot_items=3000, hot_arrivals=60000)
            wh = Warehouse.load(wh_dir)
            # 找到热点键的一个物理ID（夹具中热点单据紧随构造案例之后）
            hot_doc = None
            for r in wh.tables["documents"]:
                if (r["supplier"], r["doc_no"]) == ("STARK", "PO-9000"):
                    hot_doc = r["doc_row_id"]
                    break
            requests = [{"request_id": "HOT", "doc_row_id": hot_doc}]
            pkg_dir = os.path.join(tmp, "pkg")
            t0 = time.monotonic()
            slice_package(wh, requests, pkg_dir)
            t_slice = time.monotonic() - t0
            t0 = time.monotonic()
            pkg = load_and_verify(pkg_dir)
            t_verify = time.monotonic() - t0
            # 包大小随必要范围线性：60000 到货 + 3000 项目 + 500 单据，无叉乘膨胀
            self.assertEqual(len(pkg.rows["arrivals"]), 60000)
            self.assertEqual(len(pkg.rows["items"]), 3000)
            self.assertEqual(len(pkg.rows["documents"]), 500)
            # 宽松时间上限（正常应远低于此）
            self.assertLess(t_slice, 30.0)
            self.assertLess(t_verify, 30.0)
            print(f"\n[hot-key] slice={t_slice:.2f}s verify={t_verify:.2f}s")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
