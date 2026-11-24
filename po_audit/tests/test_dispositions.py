"""差异处置清单测试（stdlib unittest）。

使用 snapfix 生成的三版可复现快照夹具，逐条验证：
  * 待处理项从核账结果形成、同快照重复刷新幂等不重复；
  * 事实未变保留处置、事实变化重新待处理且旧处置可查；
  * 差异消失移出当前清单但保留历史、再次出现同ID复活；
  * 单项目事实变化不重置同单其他项目；
  * 状态/说明登记、清单过滤、未知ID/非法状态/移除项禁写；
  * 确定性 discrepancy_id 与状态文件往返；
  * CLI 子命令与退出码；
  * 原有逐单报告、切片与离线复算行为不受影响（见 test_po_audit.py 全套）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from po_audit import snapfix
from po_audit.dispositions import (
    ALL_STATUSES, DispositionStore, InactiveDiscrepancyError,
    InvalidStatusError, STATUS_DISMISSED, STATUS_OPEN, STATUS_RESOLVED,
    STATUS_IN_PROGRESS, UnknownDiscrepancyError, discrepancy_id_for,
    extract_discrepancies, refresh,
)
from po_audit.warehouse import Warehouse

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_cli(*args):
    env = dict(os.environ, PYTHONPATH=PROJECT_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "po_audit.dispositions", *args],
        capture_output=True, text=True, env=env, cwd="/",
    )


class SnapshotBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="po_audit_disp_")
        snapfix.generate_multi_snapshot(cls.tmp)
        with open(os.path.join(cls.tmp, "evolution.json"), encoding="utf-8") as fh:
            cls.evolution = json.load(fh)
        with open(os.path.join(cls.tmp, "requests.json"), encoding="utf-8") as fh:
            cls.requests = json.load(fh)
        cls.wh = {}
        for v in snapfix.VERSIONS:
            cls.wh[v] = Warehouse.load(os.path.join(cls.tmp, v))
        cls.doc_ids = [r["doc_row_id"] for r in cls.requests]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def dsp(self, kind, supplier, doc_no, item_row_id=None):
        return discrepancy_id_for(kind, (supplier, doc_no), item_row_id)

    def load_warehouse(self, version):
        return Warehouse.load(os.path.join(self.tmp, version))

    def new_store(self):
        return DispositionStore.new()

    def active_ids(self, store):
        return {e["discrepancy_id"] for e in store.active_entries()}

    def refresh_all(self, store, version):
        return refresh(store, self.load_warehouse(version), self.doc_ids)


class TestRefreshLifecycle(SnapshotBase):
    def test_v1_creates_expected_discrepancies(self):
        store = self.new_store()
        s = self.refresh_all(store, "snap-v1")
        self.assertEqual(sorted(s["created"]),
                         self.evolution["expected_active"]["snap-v1"])
        self.assertEqual(s["reopened"], [])
        self.assertEqual(s["removed"], [])
        self.assertEqual(s["reappeared"], [])
        # 与逐单重算的报告事实一致
        self.assertEqual(self.active_ids(store),
                         set(self.evolution["expected_active"]["snap-v1"]))
        # 无关单 ZULU 任何差异都不得进入清单
        self.assertFalse(any(e["doc_key"]["supplier"] == "ZULU"
                             for e in store.all_entries()))

    def test_duplicate_refresh_same_snapshot_is_idempotent(self):
        store = self.new_store()
        first = self.refresh_all(store, "snap-v1")
        event_counts = {d: len(e["events"]) for d, e in store.ledger.items()}
        second = self.refresh_all(store, "snap-v1")
        # 再次刷新：无新增、无再开、无移除、无再现；全部为 unchanged
        self.assertEqual(second["counts"]["created"], 0)
        self.assertEqual(second["counts"]["reopened"], 0)
        self.assertEqual(second["counts"]["removed"], 0)
        self.assertEqual(second["counts"]["reappeared"], 0)
        self.assertEqual(second["counts"]["unchanged"], len(first["created"]))
        # 不追加任何事件、不改动处置
        for d, e in store.ledger.items():
            self.assertEqual(len(e["events"]), event_counts[d])
            self.assertEqual(e["status"], STATUS_OPEN)
        # 快照记录只增刷新计数，不重复建条目
        rec = store.snapshots["snap-v1"]
        self.assertEqual(rec["refresh_count"], 2)
        self.assertEqual(len(store.snapshots), 1)
        # 清单条目数量不变（不重复生成处置项）
        self.assertEqual(len(store.ledger), len(first["created"]))

    def test_v2_transition_actions(self):
        store = self.new_store()
        self.refresh_all(store, "snap-v1")
        s = self.refresh_all(store, "snap-v2")
        exp = self.evolution["expected_transition"]["snap-v2"]
        self.assertEqual(s["created"], sorted(exp["created"]))
        self.assertEqual(s["reopened"], sorted(exp["reopened"]))
        self.assertEqual(s["removed"], sorted(exp["removed"]))
        self.assertEqual(s["reappeared"], sorted(exp["reappeared"]))
        self.assertEqual(self.active_ids(store),
                         set(self.evolution["expected_active"]["snap-v2"]))

    def test_v3_transition_actions(self):
        store = self.new_store()
        self.refresh_all(store, "snap-v1")
        self.refresh_all(store, "snap-v2")
        s = self.refresh_all(store, "snap-v3")
        exp = self.evolution["expected_transition"]["snap-v3"]
        self.assertEqual(s["created"], sorted(exp["created"]))
        self.assertEqual(s["reopened"], sorted(exp["reopened"]))
        self.assertEqual(s["removed"], sorted(exp["removed"]))
        self.assertEqual(s["reappeared"], sorted(exp["reappeared"]))
        self.assertEqual(self.active_ids(store),
                         set(self.evolution["expected_active"]["snap-v3"]))

    def test_removed_discrepancy_leaves_active_list_but_keeps_history(self):
        store = self.new_store()
        self.refresh_all(store, "snap-v1")
        d = self.dsp("short", "ACME", "PO-A", "I-A2")
        store.record_disposition(d, STATUS_RESOLVED, "已联系供应商")
        self.refresh_all(store, "snap-v2")  # 补齐 → 移除
        entry = store.get(d)
        self.assertFalse(entry["active"])
        self.assertEqual(entry["removed_snapshot"], "snap-v2")
        self.assertNotIn(entry, store.active_entries())
        # ledger 与完整历史仍可查
        self.assertIn(d, {e["discrepancy_id"] for e in store.all_entries()})
        types = [ev["type"] for ev in entry["events"]]
        self.assertEqual(types[0], "created")
        self.assertIn("disposition", types)
        self.assertEqual(types[-1], "removed")
        # 旧处置内容保留在历史事件中
        disp_ev = [ev for ev in entry["events"] if ev["type"] == "disposition"][0]
        self.assertEqual(disp_ev["status"], STATUS_RESOLVED)
        self.assertEqual(disp_ev["note"], "已联系供应商")

    def test_reappear_uses_same_id_and_keeps_full_history(self):
        store = self.new_store()
        self.refresh_all(store, "snap-v1")
        d = self.dsp("item_suspended", "DELTA", "PO-D", "I-D1")
        store.record_disposition(d, STATUS_IN_PROGRESS, "等待质检")
        self.refresh_all(store, "snap-v2")  # 解除 → 移除
        self.assertFalse(store.get(d)["active"])
        s = self.refresh_all(store, "snap-v3")  # 再挂起 → 再现
        self.assertEqual(s["reappeared"], [d])
        entry = store.get(d)
        self.assertTrue(entry["active"])
        self.assertEqual(entry["status"], STATUS_OPEN)
        self.assertIsNone(entry["removed_snapshot"])
        types = [ev["type"] for ev in entry["events"]]
        self.assertEqual(types, ["created", "disposition", "removed", "reappeared"])

    def test_fact_change_reopens_and_old_disposition_remains_visible(self):
        store = self.new_store()
        self.refresh_all(store, "snap-v1")
        d = self.dsp("short", "GAMMA", "PO-C", "I-C1")
        store.record_disposition(d, STATUS_IN_PROGRESS, "供应商承诺补发")
        old_fp = store.get(d)["fact_fingerprint"]
        s = self.refresh_all(store, "snap-v2")  # 实到 4→5 仍短
        self.assertEqual(s["reopened"], [d])
        entry = store.get(d)
        self.assertTrue(entry["active"])
        self.assertEqual(entry["status"], STATUS_OPEN)
        self.assertEqual(entry["note"], "")
        self.assertNotEqual(entry["fact_fingerprint"], old_fp)
        # 旧处置仍可在历史中查到
        ev = [e for e in entry["events"] if e["type"] == "disposition"][0]
        self.assertEqual(ev["note"], "供应商承诺补发")
        self.assertEqual(ev["status"], STATUS_IN_PROGRESS)
        reopen_ev = [e for e in entry["events"] if e["type"] == "reopened"][0]
        self.assertEqual(reopen_ev["snapshot"], "snap-v2")
        # 新指纹对应新事实
        self.assertEqual(entry["facts"]["arrived_qty"], 5)

    def test_sibling_item_disposition_not_reset_by_other_items_fact_change(self):
        """只影响一个项目的事实变化，不得重置同单其他项目的处置。"""
        store = self.new_store()
        self.refresh_all(store, "snap-v1")
        nut = self.dsp("short", "BETA", "PO-B", "I-B1")
        bolt = self.dsp("short", "BETA", "PO-B", "I-B2")
        store.record_disposition(bolt, STATUS_RESOLVED, "BOLT 按计划分批到")
        store.record_disposition(nut, STATUS_IN_PROGRESS, "NUT 跟进中")
        self.refresh_all(store, "snap-v2")  # NUT 补齐移除；BOLT 仍短且事实未变
        # BOLT 处置原样保留
        bolt_e = store.get(bolt)
        self.assertTrue(bolt_e["active"])
        self.assertEqual(bolt_e["status"], STATUS_RESOLVED)
        self.assertEqual(bolt_e["note"], "BOLT 按计划分批到")
        # NUT 被移除但历史保留
        self.assertFalse(store.get(nut)["active"])
        # v3：BOLT 自己的事实变化只重置 BOLT
        self.refresh_all(store, "snap-v3")
        bolt_e = store.get(bolt)
        self.assertFalse(bolt_e["active"])  # BOLT 也补齐 → 移除
        # 不产生任何牵连 NUT 的新事件
        self.assertEqual(
            [ev["type"] for ev in store.get(nut)["events"]],
            ["created", "disposition", "removed"],
        )

    def test_document_level_disposition_survives_unchanged_refreshes(self):
        store = self.new_store()
        self.refresh_all(store, "snap-v1")
        dup = self.dsp("duplicate_doc_number", "EPSILON", "PO-E")
        store.record_disposition(dup, STATUS_DISMISSED, "重号已知悉，合并处理")
        self.refresh_all(store, "snap-v2")
        self.refresh_all(store, "snap-v3")
        e = store.get(dup)
        self.assertTrue(e["active"])
        self.assertEqual(e["status"], STATUS_DISMISSED)
        self.assertEqual(e["note"], "重号已知悉，合并处理")

    def test_empty_document_removed_then_item_level_discrepancy_created(self):
        store = self.new_store()
        self.refresh_all(store, "snap-v1")
        empty_id = self.dsp("empty_document", "EPSILON", "PO-E")
        self.assertTrue(store.get(empty_id)["active"])
        self.refresh_all(store, "snap-v2")
        self.assertFalse(store.get(empty_id)["active"])
        short_id = self.dsp("short", "EPSILON", "PO-E", "I-E1")
        self.assertTrue(store.get(short_id)["active"])
        # 两者自然键不同，是独立条目，历史互不覆盖
        self.assertNotEqual(empty_id, short_id)

    def test_deleted_document_reported_missing_without_new_entries(self):
        store = self.new_store()
        self.refresh_all(store, "snap-v1")
        fox_before = [e["discrepancy_id"] for e in store.all_entries()
                      if e["doc_key"]["supplier"] == "FOXTROT"]
        fox_events = {d: len(store.get(d)["events"]) for d in fox_before}
        s = self.refresh_all(store, "snap-v2")
        self.assertEqual(s["missing_doc_row_ids"], ["D-F"])
        # FOXTROT 不产生任何新条目、不追加事件；其历史原样保留
        fox_after = [e["discrepancy_id"] for e in store.all_entries()
                     if e["doc_key"]["supplier"] == "FOXTROT"]
        self.assertEqual(sorted(fox_after), sorted(fox_before))
        for d in fox_before:
            self.assertEqual(len(store.get(d)["events"]), fox_events[d])

    def test_unknown_doc_row_id_is_reported_not_raised(self):
        store = self.new_store()
        s = refresh(store, self.load_warehouse("snap-v1"),
                    self.doc_ids + ["D-NOPE-404"])
        self.assertEqual(s["unknown_doc_row_ids"], ["D-NOPE-404"])

    def test_kind_invariants_and_vocabulary(self):
        store = self.new_store()
        self.refresh_all(store, "snap-v1")
        for e in store.all_entries():
            self.assertIn(e["kind"], {
                "duplicate_doc_number", "empty_document", "document_suspended",
                "short", "over", "item_suspended",
            })
            self.assertIn(e["status"], set(ALL_STATUSES))
            self.assertTrue(e["discrepancy_id"].startswith("DSP-"))
            # 单据级无项目ID；项目级必有物理行ID
            if e["kind"] in ("short", "over", "item_suspended"):
                self.assertIsNotNone(e["item_row_id"])
            else:
                self.assertIsNone(e["item_row_id"])


class TestDispositionRecording(SnapshotBase):
    def setUp(self):
        self.store = self.new_store()
        self.refresh_all(self.store, "snap-v1")
        self.d = self.dsp("short", "ACME", "PO-A", "I-A2")

    def test_record_status_and_note(self):
        e = self.store.record_disposition(self.d, STATUS_RESOLVED, "补货完成待核")
        self.assertEqual(e["status"], STATUS_RESOLVED)
        self.assertEqual(e["note"], "补货完成待核")
        ev = e["events"][-1]
        self.assertEqual(ev["type"], "disposition")
        self.assertEqual(ev["status"], STATUS_RESOLVED)
        self.assertEqual(ev["note"], "补货完成待核")

    def test_identical_disposition_does_not_append_event(self):
        self.store.record_disposition(self.d, STATUS_IN_PROGRESS, "x")
        n = len(self.store.get(self.d)["events"])
        self.store.record_disposition(self.d, STATUS_IN_PROGRESS, "x")
        self.assertEqual(len(self.store.get(self.d)["events"]), n)

    def test_unknown_id_raises(self):
        with self.assertRaises(UnknownDiscrepancyError):
            self.store.record_disposition("DSP-deadbeefdeadbeef", STATUS_OPEN, "")

    def test_invalid_status_raises(self):
        with self.assertRaises(InvalidStatusError):
            self.store.record_disposition(self.d, "closed", "")

    def test_inactive_entry_cannot_receive_disposition(self):
        self.refresh_all(self.store, "snap-v2")  # ACME 短到消失
        with self.assertRaises(InactiveDiscrepancyError):
            self.store.record_disposition(self.d, STATUS_RESOLVED, "x")

    def test_list_filters_by_status_and_doc(self):
        other = self.dsp("short", "BETA", "PO-B", "I-B2")
        self.store.record_disposition(other, STATUS_RESOLVED, "done")
        resolved = self.store.active_entries(status=STATUS_RESOLVED)
        self.assertEqual([e["discrepancy_id"] for e in resolved], [other])
        only_a = self.store.active_entries(doc_row_id="D-A")
        self.assertTrue(only_a)
        self.assertTrue(all("D-A" in e["doc_row_ids"] for e in only_a))


class TestDeterminismAndPersistence(SnapshotBase):
    def test_discrepancy_id_stable_across_stores(self):
        d1 = discrepancy_id_for("short", ("ACME", "PO-A"), "I-A2")
        d2 = discrepancy_id_for("short", ("ACME", "PO-A"), "I-A2")
        self.assertEqual(d1, d2)
        # 同单不同项目 → 不同ID（隔离性的标识基础）
        self.assertNotEqual(
            discrepancy_id_for("short", ("BETA", "PO-B"), "I-B1"),
            discrepancy_id_for("short", ("BETA", "PO-B"), "I-B2"),
        )
        # 同项目不同种类 → 不同ID（v3 RING 的挂起与超到并存）
        self.assertNotEqual(
            discrepancy_id_for("over", ("DELTA", "PO-D"), "I-D1"),
            discrepancy_id_for("item_suspended", ("DELTA", "PO-D"), "I-D1"),
        )

    def test_evolution_natural_keys_match_generated_ids(self):
        for nk in self.evolution["natural_keys"]:
            got = discrepancy_id_for(
                nk["kind"],
                (nk["doc_key"]["supplier"], nk["doc_key"]["doc_no"]),
                nk["item_row_id"],
            )
            self.assertEqual(got, nk["discrepancy_id"])

    def test_state_roundtrip_preserves_everything(self):
        store = self.new_store()
        self.refresh_all(store, "snap-v1")
        d = self.dsp("short", "ACME", "PO-A", "I-A2")
        store.record_disposition(d, STATUS_RESOLVED, "note-持久化")
        self.refresh_all(store, "snap-v2")
        path = os.path.join(self.tmp, "state", "disp.json")
        store.save(path)
        again = DispositionStore.load(path)
        self.assertEqual(
            json.dumps(again.all_entries(), sort_keys=True, ensure_ascii=False),
            json.dumps(store.all_entries(), sort_keys=True, ensure_ascii=False),
        )
        self.assertEqual(again.scanned_doc_row_ids, store.scanned_doc_row_ids)
        self.assertEqual(again.snapshots, store.snapshots)
        self.assertTrue(again.get(d)["events"])

    def test_regenerate_fixture_is_byte_stable(self):
        """夹具可复现：同参数再生成，四表与 evolution 内容逐字节一致。"""
        other = tempfile.mkdtemp(prefix="po_audit_disp_re_")
        try:
            snapfix.generate_multi_snapshot(other)
            for v in snapfix.VERSIONS:
                for name in ("documents.jsonl", "items.jsonl", "arrivals.jsonl",
                             "suspensions.jsonl", "version.json"):
                    a = os.path.join(self.tmp, v, name)
                    b = os.path.join(other, v, name)
                    with open(a, "rb") as fa, open(b, "rb") as fb:
                        self.assertEqual(fa.read(), fb.read(), f"{v}/{name}")
            with open(os.path.join(other, "evolution.json"), "rb") as fa, \
                 open(os.path.join(self.tmp, "evolution.json"), "rb") as fb:
                self.assertEqual(fa.read(), fb.read())
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_load_rejects_foreign_format(self):
        path = os.path.join(self.tmp, "bad.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"format": "something-else", "ledger": [],
                       "scanned_doc_row_ids": [], "snapshots": []}, fh)
        with self.assertRaises(Exception):
            DispositionStore.load(path)


class TestCli(SnapshotBase):
    def setUp(self):
        # 每个测试方法独立状态文件，避免共享状态造成刷新幂等干扰
        self.state = os.path.join(self.tmp, f"cli_state_{self._testMethodName}.json")

    def _refresh(self, version):
        return run_cli("refresh", "--warehouse", os.path.join(self.tmp, version),
                       "--state", self.state,
                       "--requests", os.path.join(self.tmp, "requests.json"))

    def test_full_cli_workflow_and_exit_codes(self):
        p = self._refresh("snap-v1")
        self.assertEqual(p.returncode, 0, p.stderr)
        created = json.loads(p.stdout)["created"]
        target = self.dsp("short", "ACME", "PO-A", "I-A2")
        self.assertIn(target, created)

        # 重复刷新：created=0，退出码仍为 0
        p2 = self._refresh("snap-v1")
        self.assertEqual(json.loads(p2.stdout)["counts"]["created"], 0)

        # list 当前清单
        p = run_cli("list", "--state", self.state)
        self.assertEqual(p.returncode, 0, p.stderr)
        listing = json.loads(p.stdout)
        self.assertEqual(listing["total"], len(created))

        # 登记处置
        p = run_cli("note", "--state", self.state,
                    "--discrepancy-id", target,
                    "--status", "resolved", "--note", "线下已确认")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["status"], "resolved")

        # 状态过滤
        p = run_cli("list", "--state", self.state, "--status", "resolved")
        self.assertEqual(json.loads(p.stdout)["total"], 1)

        # history 单项
        p = run_cli("history", "--state", self.state,
                    "--discrepancy-id", target)
        self.assertEqual(p.returncode, 0, p.stderr)
        types = [ev["type"] for ev in json.loads(p.stdout)["events"]]
        self.assertIn("disposition", types)

        # show
        p = run_cli("show", "--state", self.state,
                    "--discrepancy-id", target)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(json.loads(p.stdout)["discrepancy_id"], target)

        # 刷新到 v2：该差异移除
        p = self._refresh("snap-v2")
        self.assertEqual(p.returncode, 0, p.stderr)
        # --all 仍可见历史项
        p = run_cli("list", "--state", self.state, "--all")
        ids = {d["discrepancy_id"] for d in json.loads(p.stdout)["discrepancies"]}
        self.assertIn(target, ids)
        # 默认列表不含
        p = run_cli("list", "--state", self.state)
        ids = {d["discrepancy_id"] for d in json.loads(p.stdout)["discrepancies"]}
        self.assertNotIn(target, ids)
        # 对已移除项登记处置 → 拒绝
        p = run_cli("note", "--state", self.state,
                    "--discrepancy-id", target, "--status", "open", "--note", "")
        self.assertEqual(p.returncode, 21)

    def test_cli_unknown_discrepancy_exit_code(self):
        self._refresh("snap-v1")
        p = run_cli("show", "--state", self.state,
                    "--discrepancy-id", "DSP-0000000000000000")
        self.assertEqual(p.returncode, 21)

    def test_cli_invalid_status_exit_code(self):
        self._refresh("snap-v1")
        target = self.dsp("short", "ACME", "PO-A", "I-A2")
        p = run_cli("note", "--state", self.state,
                    "--discrepancy-id", target,
                    "--status", "bogus", "--note", "")
        self.assertEqual(p.returncode, 22)

    def test_cli_refresh_with_requests_file_and_unknown_doc(self):
        p = run_cli("refresh", "--warehouse", os.path.join(self.tmp, "snap-v1"),
                    "--state", self.state,
                    "--requests", os.path.join(self.tmp, "requests.json"))
        self.assertEqual(p.returncode, 0)
        # 追加一个不存在的物理ID → 退出码 23
        req2 = os.path.join(self.tmp, "requests_bad.json")
        with open(req2, "w", encoding="utf-8") as fh:
            json.dump(self.requests + [{"request_id": "X", "doc_row_id": "D-404"}], fh)
        p = run_cli("refresh", "--warehouse", os.path.join(self.tmp, "snap-v1"),
                    "--state", self.state, "--requests", req2)
        self.assertEqual(p.returncode, 23)
        self.assertIn("D-404", json.loads(p.stdout)["unknown_doc_row_ids"])

    def test_cli_missing_state_file_errors(self):
        p = run_cli("list", "--state", os.path.join(self.tmp, "nope.json"))
        self.assertEqual(p.returncode, 20)


class TestReportUnchanged(SnapshotBase):
    """处置清单复用 compute_report；验证抽取结果与逐单报告逐项对应。"""

    def test_extracted_discrepancies_match_report_facts(self):
        from po_audit.whreport import warehouse_report
        wh = self.load_warehouse("snap-v1")
        for did in ("D-A", "D-B", "D-C", "D-D", "D-E1", "D-F"):
            rep = warehouse_report(wh, did)
            found = {(d.kind, d.item_row_id) for d in extract_discrepancies(rep)}
            expect = set()
            if rep["duplicate_doc_number"]["is_duplicate"]:
                expect.add(("duplicate_doc_number", None))
            if rep["empty_document"]:
                expect.add(("empty_document", None))
            if rep["document_suspended"]:
                expect.add(("document_suspended", None))
            for it in rep["items"]:
                if it["fulfillment"] in ("short", "over"):
                    expect.add((it["fulfillment"], it["item_row_id"]))
                if it["suspended"]:
                    expect.add(("item_suspended", it["item_row_id"]))
            self.assertEqual(found, expect, did)


if __name__ == "__main__":
    unittest.main()
