# 恢复历史版本

归档保留的是完整 Git 对象历史，不是只有 SHA 的文本清单。不要从归档 README 推断不同方案已经合并。

## 查看一个旧版本

```bash
git fetch origin archive/history
git show <BRANCH_INDEX中的完整SHA>:<仓库内文件路径>
git worktree add --detach ../optomind-history <完整SHA>
```

## 必须临时恢复旧分支时

```bash
git branch historical-check <完整SHA>
```

这只创建本地分支。没有必要时不要重新推送全部旧分支，否则会重新产生分支混乱。

## 从完整备份恢复

```bash
git clone --mirror OptoMind-before-cleanup-20261005.bundle restored.git
git -C restored.git fsck --full
```

bundle包含清理前全部分支及历史，不含未提交的本地文件、外部数据库、未上传论文和密钥。Git之外的资产不能由此恢复。main的原始SHA见BRANCH_INDEX.json。
