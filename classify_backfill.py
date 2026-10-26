#!/usr/bin/env python3
"""记忆账本分层回填 v2.0 — 给历史记录打 mem_type 标签

规则(轻量, 不调LLM, 零成本):
  preference : user消息里带"以后/记住/不要/必须/别再/口径/规则"等明确指令或纠偏
  procedure : 带"步骤/流程/按以下步骤/执行顺序"的操作规程
  state     : 带时间状态的临时状态(当前/现在/今天/本周/截至/已提交/水位)
  fact      : 其余
  tool噪音  : 排除[IMPORTANT]/sheet-mcp/cron注入等系统模板
"""
import re
import sqlite3
import sys

LEDGER_DIR = os.environ.get('LEDGER_DIR', '/opt/memory-ledger')
LEDGER = LEDGER_DIR + '/ledger.db'

PREF = re.compile(
    r'(以后|从今以后|以后都|不要再|别再|必须|记住|每次都|优先|偏好|习惯|'
    r'规则是|口径是|不能|不可以|要改|不对|错了|重来|忽略即可|不用理会|'
    r'看情况|拍板|同意|不同意|以后XX|永久)', re.S)
PROC = re.compile(
    r'(步骤|流程|先.{0,8}再|执行顺序|操作步骤|按以下步骤|严格按|依次|标准流程)', re.S)
STATE = re.compile(
    r'(当前|现在|目前|正在|今天|本周|截至|暂时|已提交|进行中|水位|'
    r'已上线|已升级|已修复|已删|已停用)', re.S)

# 系统模板(不是真实用户内容)
NOISE = re.compile(
    r'(用sheet-mcp读取在线进度表|scheduled cron job|ASYNC DELEGATION|'
    r'OUT-OF-BAND USER MESSAGE|Background process|'
    r'Gateway message origin|This is an automated|'
    r'你是资深|方案负责人|独立审核一份|按以下步骤：|严格按以下步骤)', re.S)


def strip_prefix(t):
    for pat in (r'^\[IMPORTANT[^\]]*\]\s*', r'^\[System[^\]]*\]\s*',
                r'^\[OUT-OF-BAND USER MESSAGE[^\]]*\]\s*'):
        t = re.sub(pat, '', t, flags=re.S)
    return re.sub(r'\[IMPORTANT[^\]]*\]', '', t).strip()


def classify(content, role):
    if NOISE.search(content):
        return 'noise'
    txt = strip_prefix(content)
    if not txt:
        return 'noise'
    if role in ('user', 'human'):
        if PREF.search(txt):
            return 'preference'
        if PROC.search(txt):
            return 'procedure'
    if STATE.search(txt) and len(txt) < 500:
        return 'state'
    return 'fact'


def main():
    c = sqlite3.connect(LEDGER)
    c.execute('PRAGMA busy_timeout=10000')
    rows = c.execute('SELECT id, role, content FROM ledger').fetchall()
    counts = {}
    upd = []
    for rid, role, content in rows:
        mt = classify(content or '', role)
        counts[mt] = counts.get(mt, 0) + 1
        upd.append((mt, rid))
    c.executemany('UPDATE ledger SET mem_type=? WHERE id=?', upd)
    c.commit()
    print('分层回填完成:', counts)
    # 同步 type 字段(现有query用type做半衰期), 让分层真正生效
    c.execute("UPDATE ledger SET type=mem_type WHERE mem_type IN ('preference','procedure','state')")
    c.execute("UPDATE ledger SET type='fact' WHERE mem_type IS NULL OR mem_type NOT IN "
              "('preference','procedure','state')")
    c.commit()
    print('type字段同步:', c.execute(
        'SELECT type, count(*) FROM ledger GROUP BY type').fetchall())
    c.close()


if __name__ == '__main__':
    main()
