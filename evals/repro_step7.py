"""重放会话里中断的那一步，记录每个 chunk 的到达时间与类型。

用途：判断"断流"是客户端读超时（长时间无数据）还是模型自己结束。
输出：每一步的间隔秒数、是否含 content / thinking / tool_calls、最后一个 chunk 的性质。
"""
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import tools  # noqa: F401  触发工具注册
from agent.client import OllamaClient
from agent.config import AgentConfig
from agent.registry import all_specs

SESSION = pathlib.Path.home() / ".ollama-agent" / "sessions" / "20261101-222440-bo7h.json"

if not SESSION.is_file():
    # 这是针对某一次断流的一次性排障脚本，会话文件不在了就没法重放。
    # 与其抛一屏 FileNotFoundError，不如说清怎么换一个会话。
    import glob
    cands = sorted(glob.glob(str(SESSION.parent / "*.json")))[-3:]
    print(f"找不到要重放的会话：{SESSION}")
    print("用法：把本文件里 SESSION 改成要分析的会话文件。现有会话（最近 3 条）：")
    for c in cands:
        print("  ", c)
    sys.exit(1)

d = json.loads(SESSION.read_text(encoding="utf-8"))
history = d["history"]
print(f"历史 {len(history)} 条，模型 {d['model']}，工作空间 {d['workspace']}")

cfg = AgentConfig(model=d["model"], workspace=d["workspace"])
client = OllamaClient(cfg)

print("发出请求…")
t0 = time.time()
resp = client._post_stream(history, all_specs())

last = t0
gaps = []
n_content = n_think = n_tc = 0
content_len = 0
tool_names = []
done_obj = None

for raw in resp:
    now = time.time()
    gap = now - last
    last = now
    line = raw.decode("utf-8", "replace").strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        continue
    msg = obj.get("message") or {}
    ct = msg.get("content") or ""
    th = msg.get("thinking") or ""
    tcs = msg.get("tool_calls") or []
    if ct:
        n_content += 1
        content_len += len(ct)
    if th:
        n_think += 1
    if tcs:
        n_tc += 1
        for tc in tcs:
            fn = (tc.get("function") or {}).get("name")
            args = (tc.get("function") or {}).get("arguments")
            tool_names.append((fn, len(json.dumps(args, ensure_ascii=False)) if args else 0))
    if gap > 2.0:
        gaps.append((round(now - t0, 1), round(gap, 1)))
        print(f"  [静默 {gap:5.1f}s] t={now - t0:6.1f}s  content累计={content_len} chunk#{n_content}")
    if obj.get("done"):
        done_obj = obj

total = time.time() - t0
print("\n==== 汇总 ====")
print(f"总耗时 {total:.1f}s")
print(f"content chunk {n_content} 个，共 {content_len} 字")
print(f"thinking chunk {n_think} 个")
print(f"tool_calls chunk {n_tc} 个 -> {tool_names}")
print(f"收到 done 帧: {bool(done_obj)}")
if done_obj:
    print(f"  done_reason={done_obj.get('done_reason')} eval_count={done_obj.get('eval_count')} "
          f"prompt_eval_count={done_obj.get('prompt_eval_count')}")
print(f"大于 2 秒的静默 {len(gaps)} 次，最长 {max([g[1] for g in gaps], default=0):.1f}s")
print(f"最长静默明细: {gaps[-3:] if gaps else '无'}")
