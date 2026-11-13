# 04-changecast（变投影）

Changecast — 变更日志投影：源变更流 → 按序投影 → 乱序/晚到策略钩子 → 快照可查互证。

与 Feedstitch（多源 Feed 归并）、Batchgate（作业依赖编排）、Syncledger（双端对账）主链路不同。夹具全本地，不连活库。

## 启动

```bash
docker compose up --build
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://localhost:5400 |
| API | http://localhost:10400 |

## 主链

配置投影流 → 读取 CDC 夹具并按 seq 应用到投影表 → 批次计数与快照互证

## 种子投影流

- **member_profile**：会员档案变更（`member_id`，字段 `display_name` / `tier` / `region`）
- **store_archive**：门店档案变更（`store_id`，字段 `name` / `status` / `city`）
- **channel_attr**：渠道属性变更（`channel_code`，字段 `label` / `active`）

底座默认按 **事件 seq 升序** 投影；乱序缓冲表空挂，深逻辑留给后题。对外文案不用「工单」「CRM」。

## 键变更（04-key-rename-migrate）

源侧以独立 op 发「实体键变更」事件：`op="rename"`，`entity_key`=旧键，`new_entity_key`=新键。
不当普通 upsert/delete 处理（那会丢 applied_seq 血缘或造成双键并存）。

互斥策略（设置 `key_change_policy`，二者居一，可在「设置」页切换）：

- **迁移（migrate）**：把旧键投影行**原子改挂**到新键——保留载荷，行上记录
  `renamed_from` / `prev_applied_seq` 血缘，applied_seq 语义可追溯；旧键退出存活快照，
  不留墓碑、不双键并存。迁移不成立则**整事件失败**，新旧两键状态与开跑前一致。
- **拒绝迁移（reject）**：不改投影，事件进隔离留痕（`key_change_traces`），旧键行保持不动。

新键已有**存活**投影行时的细化规则（本实现贯彻其一）：**冲突拒绝迁移**——不并入、不覆盖，
事件进隔离留痕（reason=`new_key_conflict`），两键不动。其余拒绝情形：
`old_key_not_live`（旧键无存活行）、`same_key`、`missing_new_key`、`policy_reject`。

禁止以「先删旧再插新」冒充迁移（会丢 seq/事件血缘）；禁止迁移成功后旧键与新键同时存活。

互证（「键变更」页 `/key-changes`，接口 `/api/key-changes/overview`）：

1. 批次计数（`applied_rename` / `rejected_rename` 累计）↔ 留痕条数逐批互证；
2. 迁移成功的旧键不得出现在存活快照键集合；
3. 迁移成功的新键必须在快照键集合内。

夹具演示场景：`member_profile` 含迁移成功（M1001→M1101）、新键冲突（M1003→M1005）、
旧键已删（M1002→M1201）；`store_archive`（S03→S03A）、`channel_attr`（WEB→WEB2）为迁移成功。
切到 reject 策略重跑，可演示全部事件进隔离、投影不动。

测试：`cd backend && python -m pytest tests/`

## 技术栈

Python 3.12 + FastAPI + SQLite；Vue 3 + Vite + Nginx。

## 0-1 切口（未实现，见出题规划）

`arrival_vs_event_order` / `ooo_window` / `event_idempotency` / `tombstone_policy` / `watermark_rewind` / …
