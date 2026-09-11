#!/usr/bin/env bash
# CNB 流水线：把 update.py 生成的产物提交回本仓库。
# 逻辑放 shell 里是为了绕开 CNB YAML 对 ": " 等特殊字符的解析问题（曾把提交行解析成 [object Object]）。
# 防死循环依赖 commit message 里的 [skip ci]（CNB 官方文档确认支持）。
set -e
git config user.name "cnb-pipeline[bot]"
git config user.email "cnb-pipeline@cnb.cool"
git add output README.md
if git diff --cached --quiet; then
  echo "产物无变化，跳过提交"
  exit 0
fi
git commit -m "auto: update aggregated sources [skip ci]"
URL=$(git remote get-url origin)
SLUG=$(printf '%s' "$URL" | sed -E 's#https?://([^@]+@)?cnb\.cool/##; s#\.git$##')
git push "https://cnb:${CNB_TOKEN}@cnb.cool/${SLUG}.git" HEAD:main
echo "已推送产物到 cnb.cool/${SLUG}"
