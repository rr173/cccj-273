"""完整仓装载、一次性索引与逐案必要范围计算。

性能约束：大表与热点键不得逐案重扫原表。装载时对每张表各做一次全表扫描
建立索引；之后每个案件只通过索引定位必要行，代价为 O(必要范围大小)。
到货按三项键 (supplier, doc_no, item_no) 索引，重复项目行不会导致
"项目×到货" 全组合物化。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from . import model


class WarehouseError(ValueError):
    pass


@dataclass
class CaseScope:
    """一个案件（点名单据物理ID）的必要范围：恰为同单键全部单据/项目、
    命中项目键的全部到货、以及适用挂起。"""
    request_id: str
    doc_row_id: str
    doc_key: tuple                       # (supplier, doc_no)
    documents: list                      # 同单键全部单据行
    items: list                          # 同单键全部项目行
    arrival_keys: list                   # [(item_key, [到货行...])] 逐项目键
    suspensions: list                    # 适用挂起行

    def rows(self):
        """产出 (table, row)，用于许可检查与收行。"""
        for r in self.documents:
            yield "documents", r
        for r in self.items:
            yield "items", r
        for _key, arr_rows in self.arrival_keys:
            for r in arr_rows:
                yield "arrivals", r
        for r in self.suspensions:
            yield "suspensions", r


class Warehouse:
    def __init__(self, version: str, tables: dict):
        self.version = version
        self.tables = tables  # table -> [row]
        # 一次性建索引（每表各一遍扫描）
        self.docs_by_id = {}
        self.docs_by_key = {}
        for r in tables["documents"]:
            self.docs_by_id[r["doc_row_id"]] = r
            self.docs_by_key.setdefault(model.doc_key_of(r), []).append(r)

        self.items_by_id = {}
        self.items_by_doc_key = {}
        for r in tables["items"]:
            self.items_by_id[r["item_row_id"]] = r
            self.items_by_doc_key.setdefault(model.doc_key_of(r), []).append(r)

        self.arrivals_by_id = {}
        self.arrivals_by_item_key = {}
        for r in tables["arrivals"]:
            self.arrivals_by_id[r["arrival_row_id"]] = r
            self.arrivals_by_item_key.setdefault(model.item_key_of(r), []).append(r)

        self.susps_by_id = {}
        self.susps_by_doc_key = {}
        for r in tables["suspensions"]:
            self.susps_by_id[r["susp_row_id"]] = r
            self.susps_by_doc_key.setdefault(model.doc_key_of(r), []).append(r)

    @classmethod
    def load(cls, root: str) -> "Warehouse":
        vpath = os.path.join(root, "version.json")
        if not os.path.isfile(vpath):
            raise WarehouseError(f"缺少版本文件: {vpath}")
        with open(vpath, "r", encoding="utf-8") as fh:
            vinfo = json.load(fh)
        version = vinfo.get("version")
        if not isinstance(version, str) or not version:
            raise WarehouseError("version.json 缺少有效的 version 字段")
        tables = {}
        for t in model.TABLES:
            p = os.path.join(root, f"{t}.jsonl")
            if not os.path.isfile(p):
                raise WarehouseError(f"缺少表文件: {p}")
            tables[t] = model.load_table(p, t)
        return cls(version, tables)

    def compute_scope(self, request_id: str, doc_row_id: str):
        """必要范围（全部经索引定位，不重扫原表）。

        返回 CaseScope；点名的物理ID不存在时返回 None。
        """
        doc = self.docs_by_id.get(doc_row_id)
        if doc is None:
            return None
        key = model.doc_key_of(doc)
        documents = list(self.docs_by_key.get(key, ()))
        items = list(self.items_by_doc_key.get(key, ()))
        item_nos = {it["item_no"] for it in items}
        arrival_keys = []
        for ino in sorted(item_nos):
            k3 = (key[0], key[1], ino)
            arrival_keys.append((k3, list(self.arrivals_by_item_key.get(k3, ()))))
        suspensions = [
            s for s in self.susps_by_doc_key.get(key, ())
            if s["item_no"] is None or s["item_no"] in item_nos
        ]
        return CaseScope(
            request_id=request_id,
            doc_row_id=doc_row_id,
            doc_key=key,
            documents=documents,
            items=items,
            arrival_keys=arrival_keys,
            suspensions=suspensions,
        )
