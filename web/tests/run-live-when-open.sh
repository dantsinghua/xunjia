#!/usr/bin/env bash
# 等 DM 真实数据恢复流动(近 30s 有 ≥5 条新行)后,跑真实数据端到端测试并对库核算。最长等 2 小时。
set -eu
for qb_name in QB_MYSQL_HOST QB_MYSQL_PORT QB_MYSQL_USER QB_MYSQL_PASSWORD QB_MYSQL_DB; do
  if [ -z "${!qb_name:-}" ]; then
    echo "Missing required configuration: $qb_name" >&2
    exit 2
  fi
done
PY=${XUNJIA_QA_PYTHON:-/home/anlin/work/xunjia-agent/.venv/bin/python}
export NODE_PATH=/tmp/claude-1000/-mnt-c-Users-anlin-Desktop-work/c31d2e3c-e05b-4634-8473-1be15ef0d8c9/scratchpad/pw/node_modules
cd ~/work/xunjia-agent
for i in $(seq 1 240); do
  n=$($PY -c "
import os,pymysql;c=pymysql.connect(host=os.environ['QB_MYSQL_HOST'],port=int(os.environ['QB_MYSQL_PORT']),user=os.environ['QB_MYSQL_USER'],password=os.environ['QB_MYSQL_PASSWORD'],database=os.environ['QB_MYSQL_DB'],connect_timeout=10,read_timeout=30);cur=c.cursor()
cur.execute('select count(*) from qb_live_best_quote where capture_time > now()+interval 8 hour - interval 30 second');print(cur.fetchone()[0])")
  if [ "${n:-0}" -ge 5 ]; then echo "$(date +%T) 真实数据已恢复流动(近30s $n 条),开始测试"; break; fi
  sleep 30
done
[ "${n:-0}" -ge 5 ] || { echo "等了 2 小时仍无真实数据,放弃"; exit 3; }
sleep 60   # 让流稳定一会
node web/tests/qb-live-e2e.cjs 180 && $PY web/tests/qb_live_e2e_check.py
