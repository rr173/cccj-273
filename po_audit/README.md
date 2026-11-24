# po_audit — 采购单复核切片与离线复算

审查方只应拿到复核指定采购单所必需的**原行**。本系统从完整仓（同版四表）按
"点名单据物理ID"切出必要范围，生成带仓主范围清单的交付包，并提供**离线复算
入口**：审查方在离线环境检验内容完整性、并从切片原行独立重算复核报告。

任一必要行受限则整案拒发——**绝不删掉受限证据后宣称合格**。

## 数据模型（同版四表，物理行ID各表唯一）

| 表 | 字段（除标识外） | 键 |
|---|---|---|
| `documents` 单据 | `doc_row_id`, `supplier`, `doc_no` | 供应方/单号关联，组合可重号 |
| `items` 项目 | `item_row_id`, `supplier`, `doc_no`, `item_no`, `expected_qty`(≥0) | 单键 + 项目号 |
| `arrivals` 到货 | `arrival_row_id`, `supplier`, `doc_no`, `item_no`, `qty`(≥0) | 三项键给非负数量 |
| `suspensions` 挂起 | `susp_row_id`, `supplier`, `doc_no`, `item_no`(可空), `reason` | 见挂起规则 |

每行另有交付许可字段：`permit`(bool)；`permit=false` 时 `restriction` 必须给出
受限类别（字符串）。仓目录含 `version.json`（同版标识）与四张 JSONL 表；
JSON 原生区分 `null` 与 `""`。

语义规则：

- **挂起**：挂起行 `item_no` 为 `null` → 整单挂起；否则仅挂起同项目号项目。
  **空字符串不是空值**：`item_no=""` 只命中项目号为 `""` 的项目。
- **到货贡献**：一份到货对每个同项目行各贡献一次；先按三项键聚合再供项目行
  查询，绝不因连接扇出乘量，也不物化"项目×到货"全组合。
- **报告**（对点名单据物理ID，各项互不掩盖、同时给出）：重号（同键单据物理行
  数）、空单（无项目行）、逐项实到（无到货为 0）、短/齐/超到
  （`short`/`exact`/`over`）、挂起（整单/同项目）。

## 必要范围与交付许可

一个案件（一次点名）的必要范围**恰为**：

1. 同 `(supplier, doc_no)` 单键的**全部**单据行与项目行；
2. 命中本案项目键的**全部**到货行（不命中任何项目的到货不入包）；
3. **适用**挂起：同单键且 `item_no` 为 `null`，或 `item_no` 命中本案项目号。

同一原行被多案共享时只交一份（包内物理去重），不带任何其他行。
切片前逐行检查交付许可：**任一必要行受限 → 整案拒发**；其他独立案件仍可交付。
拒发条目只含请求ID与受限类别，不外露受限行身份、数量或内容。

## 快速开始

```bash
cd /workspace/po_audit

# 1. 生成夹具仓（确定性；含重号/空单/空项目号/受限行/热点键）
python3 -m po_audit.fixtures --out /tmp/wh --seed 7

# 2. 切片生成交付包（可选 --hmac-key-file 由仓主签署范围清单）
python3 -m po_audit.slicer --warehouse /tmp/wh \
    --requests /tmp/wh/requests.json --package /tmp/pkg

# 3. 离线复算
python3 -m po_audit.offline --package /tmp/pkg verify            # 完整性检验
python3 -m po_audit.offline --package /tmp/pkg requests          # 清单内全部请求
python3 -m po_audit.offline --package /tmp/pkg report --request REQ-A
python3 -m po_audit.offline --package /tmp/pkg rows   --request REQ-A

# 4. 仓侧参照（可信方在完整仓上直接计算，用于交叉核对）
python3 -m po_audit.whreport --warehouse /tmp/wh --doc-row-id D-000001
```

运行测试：`python3 -m unittest discover -s tests -v`

## 交付包格式

```
package/
  manifest.json      仓主范围清单（可选 HMAC-SHA256 签名）
  documents.jsonl    切片原行（四表各一文件，跨案去重）
  items.jsonl
  arrivals.jsonl
  suspensions.jsonl
```

`manifest.json`：

- `files`：每表文件路径、SHA-256、行数；
- `row_hashes`：每一交付行的规范化 JSON SHA-256；
- `cases[]`：逐案条目
  - `delivered`：`doc_row_id`、`doc_key`、`queries[]`（逐查询：表、键、行ID列表、
    `empty` 标记）、`empty_queries`（**范围内确实无行的查询被显式列出**，如
    `items`、`arrivals[item_no='X']`）；到货查询逐项目键恰有一条，无行的项目键
    显式声明为空；
  - `refused`：仅 `request_id` + `restricted_categories`；
  - `unknown_request`：点名的物理ID在仓中不存在。

## 离线程序的保证

- **缺文件 / 篡改 / 删行 → 拒绝**（退出码 10）：文件哈希、行哈希、行数、
  清单与内容一致性（无漏报、无夹带、无未申报行）全部核对通过才输出。
- **越界查询 → 拒绝**（退出码 11）：只回答范围清单内的请求ID。
- **报告完全由包内原行重算**（与仓侧共用同一 `compute_report`），不信任任何
  预算结果。
- 退出码：`0` 成功；`10` 完整性失败；`11` 越界查询；`12` 案件被拒发；
  `13` 单据物理ID在仓中不存在。

## 信任边界

范围清单来自**可信仓主**（可经 HMAC 密钥或带外通道确认其真实性）。离线程序
检验"内容与清单一致"，**不要求、也不试图证明仓主诚实**：仓主对"范围内哪些
查询确实无行"的声明属于其可信陈述，离线端只能核对交付内容不与该声明矛盾。

## 性能设计

- 仓装载时每表各扫描一次建立索引（单键→行、三项键→行）；之后**逐案只做索引
  定位**，代价为 O(必要范围)，大表与热点键不逐案重扫原表。
- 到货按三项键聚合，重复项目行不触发"项目×到货"全组合物化；共享原行只收一份。
- 离线检验同样先对包内行建索引再逐查询核对。
- 参考：6 万行到货的热点键，切片 ≈1.2s、离线检验 ≈1.2s（见测试
  `TestHotKeyPerformance`）。
- 夹具可由 `po_audit.fixtures` 按需生成（规模参数可调），用于复现与压测。

## 目录结构

```
po_audit/
  po_audit/
    model.py       四表模式、行校验、规范哈希
    warehouse.py   完整仓装载、一次性索引、必要范围计算
    report.py      复核报告计算（仓侧与离线共用）
    slicer.py      切片器：许可检查、拒发、交付包与范围清单
    offline.py     离线复算入口：检验 + 重算报告
    whreport.py    仓侧参照报告
    fixtures.py    可生成夹具
  tests/test_po_audit.py
  README.md
```
