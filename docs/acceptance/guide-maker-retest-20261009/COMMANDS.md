# Commands and scope

所有命令均在固定 worktree 或本输出根执行，解释器为 `C:\Anaconda\python.exe -X utf8`。路径中的 `<LOCAL_KEY_FILE_NOT_PACKAGED>` 只代表本机原 key 文件，不是包内文件。

## 免费预算包装器

```powershell
C:\Anaconda\python.exe -X utf8 run_retest.py selftest
C:\Anaconda\python.exe -X utf8 run_retest.py preview
```

`selftest` 覆盖越额阻断、恢复保留费用、S0 不重基线、Max 拒绝、权威 runtime factory 收紧、网络边界拒发、原账本与 marker 不变。`preview` 只生成真实首条消息预览，不初始化 live client。

## 专项离线测试

```powershell
C:\Anaconda\python.exe -X utf8 -m pytest -q tests/upgrade3/test_guide_maker_contracts.py tests/upgrade3/test_guide_maker.py tests/upgrade3/test_guide_maker_cli.py tests/upgrade3/test_guide_maker_dedicated_budget.py tests/upgrade3/test_guide_maker_prompt.py
```

结果是 90 passed、1 个既有 Windows 临时 SQLite 删除锁失败；原始日志保存在 `free_tests/pytest_guide_maker.log`。

## 唯一真实冷启动（已执行一次，禁止重跑）

```powershell
C:\Anaconda\python.exe -X utf8 run_retest.py run-live
```

该命令绑定原 `guide_budget.sqlite`、原 marker、原 `PREPARED_MANIFEST.json`、原 tokenizer 与本机 `api_keys\qwen-api-key.txt`；wrapper 内部固定 Plus 和本轮 cap。不得为补读、提示修改或 writer 付费再次执行。

## 免费作者接口预览

```powershell
C:\Anaconda\python.exe -X utf8 scripts\upgrade3\guided_body_writer.py `
  --manifest "F:\OptoMind-Review-2\outputs\evidence_body_round2_20261008_60cny\PREPARED_MANIFEST.json" `
  --guide "F:\OptoMind-Review-2\outputs\guide_maker_retest_20261009_10cny\cold_start_live\GUIDE.json" `
  --config config\guided_body_writer\plus_first.json `
  --tokenizer "F:\OptoMind-Review-2\data\tokenizers\qwen3_5_9b\tokenizer.json" `
  --output "F:\OptoMind-Review-2\outputs\guide_maker_retest_20261009_10cny\writer_preview"
```

本命令没有 `--run`、`--budget-ledger`、`--budget-limit` 或 `--key-file`，只生成 preview，`model_calls=0`、`paid_dispatch_count=0`。
