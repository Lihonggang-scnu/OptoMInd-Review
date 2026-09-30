# 06 集成交付配置目录

本目录全部内容为**交付配置 + 人工标注 fixture**，不是模型输出，不代表真实科研内容。

## 配置 schema（review_v2_delivery.config.v1）

| 字段 | 说明 |
|---|---|
| `language` | 交付语言（zh） |
| `research_question` | 研究问题，03 首尾部件构造消息必需 |
| `chapter_roles` | 章节角色列表（chapter_id/title/role），02/03 消息使用 |
| `text_edit` | 02 编辑输入：`{"fixture": 相对路径}` 或 `{"recordings": 相对路径}`，二者恰选其一 |
| `front_back` | 03 首尾输入：同上 |
| `identity_catalogs` | 04 身份目录：`{"path": …}` 或内联列表 |
| `figure_assets` | 04 挂图条目：figure_id/path/caption/anchor_probe；path 相对本配置文件 |
| `table_moves` / `figure_moves` | 交叉引用移动规则（可选） |
| `compile_pdf` | 是否本地编译 PDF（不联网装依赖） |

所有路径相对本配置文件所在目录。缺某阶段输入时该阶段明确报 pending，
不会静默退回“仅装配”。

## 正式命令（在仓库根目录执行）

```bash
# history 起点（全链，含一次本地 PDF 编译验收）
python run_review_harness.py --delivery-start history \
  --delivery-manifest outputs/unit_writing/20260927_astra_repair/DELIVERY_MANIFEST.json \
  --delivery-batch-root outputs/full_review_draft/20260927_run01 \
  --delivery-out outputs/review_v2_delivery/06_integration \
  --delivery-config outputs/review_v2_delivery/06_integration/configs/HISTORY_DELIVERY_CONFIG.json

# plan 起点（受限交付，C6_U04 缺录制保持 pending）
python run_review_harness.py --delivery-start plan \
  --delivery-packet outputs/review_v2_repair/05_real_test_20260929/attempt02_production_path/UPDATED_WRITER_PACKET.json \
  --delivery-recordings outputs/review_v2_delivery/plan/recordings_real_03.json \
  --delivery-out outputs/review_v2_delivery/06_integration \
  --delivery-config outputs/review_v2_delivery/06_integration/configs/PLAN_DELIVERY_CONFIG.json
```

重新生成配置：`python outputs/review_v2_delivery/06_integration/configs/make_06_configs.py`
