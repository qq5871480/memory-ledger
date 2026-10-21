"""把 fallback 横幅+原因词汉化（Hermes 升级后重打）。锚基于官方 v2026.9.24。"""
import sys

path = sys.argv[1]
src = open(path).read()

if "模型切换提醒" in src:
    print("[skip] patch already present")
    sys.exit(0)

# 1) 横幅主文案
old1 = (
    'f"\u26a0\ufe0f Model fallback: {old_model} via {old_provider} unavailable "'
)
if old1 not in src:
    # 尝试直接unicode形态
    old1 = 'f"⚠️ Model fallback: {old_model} via {old_provider} unavailable "'
new1 = 'f"⚠️ 模型切换提醒：主模型 {old_model}（{old_provider}）暂时不可用 "'
if old1 not in src:
    sys.exit("[FAIL] banner anchor not found - upstream changed, re-inspect")
src = src.replace(old1, new1, 1)

# 2) 尾句
old2 = 'f"({_fallback_reason_text(reason)}); using {fb_model} via {fb_provider}."'
new2 = 'f"（{_fallback_reason_text(reason)}），已自动改用备用模型 {fb_model}（{fb_provider}）回答，不影响使用。"'
if old2 not in src:
    sys.exit("[FAIL] tail anchor not found")
src = src.replace(old2, new2, 1)

# 3) 原因词表（英文→中文，逐对替换）
pairs = {
    '"authentication permanently failed"': '"认证永久失效"',
    '"authentication failed"': '"认证失效"',
    '"billing or quota exhausted"': '"费用或额度用尽"',
    '"upstream model rate limit"': '"上游模型限流"',
    '"provider overloaded"': '"服务商过载"',
    '"provider server error"': '"服务商服务器错误"',
    '"request timeout"': '"请求超时"',
    '"TLS certificate verification failed"': '"TLS证书校验失败"',
    '"context window exceeded"': '"超出上下文窗口"',
    '"request payload too large"': '"请求体过大"',
    '"image payload too large"': '"图片过大"',
    '"model not found"': '"模型不存在"',
    '"provider policy blocked the request"': '"被服务商策略拦截"',
    '"content policy blocked the request"': '"被内容策略拦截"',
    '"request format rejected"': '"请求格式被拒"',
    '"adjacent same-role messages rejected"': '"相邻同角色消息被拒"',
    '"encrypted reasoning state rejected"': '"加密推理状态被拒"',
    '"multimodal tool content unsupported"': '"不支持多模态工具内容"',
    '"thinking signature rejected"': '"思考签名被拒"',
    '"long-context tier unavailable"': '"长上下文档位不可用"',
    '"OAuth long-context beta unavailable"': '"OAuth长上下文Beta不可用"',
    '"grammar pattern rejected"': '"语法模式被拒"',
    '"provider failure"': '"服务商故障"',
}
missed = [k for k in pairs if k not in src]
for k, v in pairs.items():
    if k in src:
        src = src.replace(k, v)
# rate limit单独处理（避免误替换upstream那条）
if '"rate limit"' in src:
    src = src.replace('"rate limit"', '"触发限流"')

open(path, "w").write(src)
print(f"[ok] patched (missed labels: {missed if missed else 'none'})")
