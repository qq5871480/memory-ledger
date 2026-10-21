#!/usr/bin/env python3
"""告警推送（可插拔）：默认仅记日志，配环境变量后走webhook。

环境变量:
  ALERT_WEBHOOK_URL  HTTP POST JSON {"text": "..."} (可选，如企业微信/钉钉/自建通道)
  ALERT              告警文本（由selfcheck.sh传入）
"""
import json, time, os, urllib.request

text = '⚠️' + os.environ.get('ALERT', 'memory ledger selfcheck alert')
url = os.environ.get('ALERT_WEBHOOK_URL', '')
if url:
    req = urllib.request.Request(url, data=json.dumps({'text': text}).encode(),
                                 headers={'Content-Type': 'application/json'})
    urllib.request.urlopen(req, timeout=10)
    print('alerted via webhook:', text)
else:
    print('[alert-not-sent, no ALERT_WEBHOOK_URL]', text)
