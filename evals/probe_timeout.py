"""验证 urllib 的 timeout 在流式生成期间是「总时长」还是「单次静默」。

做法：把 timeout 压到 10 秒，让模型生成一段需要 60 秒以上的文字。
- 若 10 秒左右抛 timeout  → timeout 会掐断活跃的流，长回合必死
- 若正常读到 done          → timeout 只在真正无数据时生效
"""
import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from agent.client import OllamaClient
from agent.config import AgentConfig

cfg = AgentConfig(timeout=10, think="off", num_ctx=4096)
client = OllamaClient(cfg)

msgs = [{"role": "user",
         "content": "请写一段 800 字左右的中文文章，主题是自动化的价值，不要用列表，写成连续段落。"}]

print("timeout=10s，开始请求…")
t0 = time.time()
chunks = 0
chars = 0
try:
    resp = client._post_chat(client._base_payload(msgs))
    for raw in resp:
        chunks += 1
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        import json
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        chars += len((obj.get("message") or {}).get("content") or "")
        el = time.time() - t0
        if chunks % 20 == 0:
            print(f"  t={el:5.1f}s chunk={chunks} chars={chars}")
        if obj.get("done"):
            print(f"  done 帧到达，t={el:.1f}s")
            break
    print(f"\n结果: 正常读完，耗时 {time.time() - t0:.1f}s，{chunks} chunk，{chars} 字")
except Exception as e:
    el = time.time() - t0
    print(f"\n结果: 异常 {type(e).__name__}: {e}")
    print(f"     在 t={el:.1f}s 发生，已收 {chunks} chunk / {chars} 字")
