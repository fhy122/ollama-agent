# 第 1 章 · 35 行接通 Ollama

**本章产出**：`v01_hello.py`，一个能对话的最小程序。约 35 行，零依赖。

---

## 一、先把 `/api/chat` 当成普通 HTTP 接口

抛开"AI"这层滤镜，Ollama 就是一个本地 HTTP 服务。`POST http://localhost:11434/api/chat` 收一个 JSON，回一个 JSON：

**请求**

```json
{
  "model": "qwen3.5:9b",
  "messages": [
    {"role": "system", "content": "你是一个助手"},
    {"role": "user", "content": "你好"}
  ],
  "stream": false
}
```

**响应**

```json
{
  "model": "qwen3.5:9b",
  "message": {"role": "assistant", "content": "你好！有什么可以帮你的？"},
  "done": true,
  "eval_count": 18,
  "prompt_eval_count": 26,
  "total_duration": 1234567890
}
```

就这些。`messages` 是**完整的历史**（不是只有最新一句），每次请求都要把整段对话发过去 —— 模型本身是无状态的，这是很多人第一个误解。

先用 curl 确认服务活着：

```bash
curl http://localhost:11434/api/chat -d '{"model":"qwen3.5:9b","messages":[{"role":"user","content":"说一句话"}],"stream":false}'
```

拿到 JSON 就说明环境没问题，可以写代码了。

---

## 二、35 行代码

新建 `v01_hello.py`：

```python
"""第 1 步：最小可运行的 Ollama 对话。"""
from __future__ import annotations

import json
import sys
import urllib.request

HOST = "http://localhost:11434"
MODEL = "qwen3.5:9b"


def chat(messages: list[dict], model: str = MODEL, host: str = HOST) -> dict:
    """一次对话往返，返回 Ollama 的原始响应 dict。"""
    payload = {"model": model, "messages": messages, "stream": False}
    req = urllib.request.Request(
        host.rstrip("/") + "/api/chat",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=1800) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> None:
    question = " ".join(sys.argv[1:]) or "用一句话介绍你自己"
    resp = chat([{"role": "user", "content": question}])
    print(resp["message"]["content"])
    print(f"--- tokens: prompt={resp.get('prompt_eval_count')} "
          f"gen={resp.get('eval_count')} "
          f"用时 {(resp.get('total_duration') or 0) / 1e9:.1f}s")


if __name__ == "__main__":
    main()
```

### 逐段说明为什么这么写

| 代码 | 为什么 |
|---|---|
| `ensure_ascii=False` | 中文不转成 `\u4f60`，请求体小一半，抓包也好读 |
| `host.rstrip("/")` | 用户传 `http://localhost:11434/`（带斜杠）时不会拼出 `//api/chat` |
| `timeout=1800` | **这是两次收到数据之间的等待上限，不是整轮总时长** —— 只要模型还在吐字，计时就重置。设 `None` 则永久阻塞（Ollama 挂死时救不回来） |
| `with urlopen(...)` | 一定要关连接。Ollama 感知到客户端断开才会停止生成 |
| `resp["message"]["content"]` | 注意嵌套：响应顶层是 `message`，不是 `content` |
| `1e9` | Ollama 的 `total_duration` 单位是**纳秒**，不是秒（第一次看到 `1234567890` 会以为是 12 亿秒） |

---

## 三、验证

```bash
python v01_hello.py "你好，介绍一下你自己"
```

期望：等 1–30 秒（首次要加载模型），然后一次性打印整段回答 + token 统计。

再试一个需要推理的：

```bash
python v01_hello.py "用三行解释什么是工具调用"
```

---

## 四、这个版本有三个明显缺陷

跑通了，但你一定已经感到不舒服：

**1. 全程干等，一个字都不出**

模型生成 300 个字要 20 秒，这 20 秒里屏幕完全静止。用户会以为程序卡死，然后 Ctrl+C —— 而 Ctrl+C 在这个版本里会让整个进程退出，连历史都没了。

**2. 没有任何错误处理**

模型名写错、Ollama 没启动、显存不够 —— 全都是一个 `urllib.error.URLError` 或 `HTTPError` 糊在脸上，看不出到底哪错了：

```
urllib.error.HTTPError: HTTP Error 404: Not Found
```

（真实情况是：模型不存在。响应体里写着 `model 'qwen3.5:9bb' not found`，但没人去读它。）

**3. 什么都塞在一个文件里**

配置、HTTP、UI 混在一起。想换个模型要改代码；想加"思考模式"要动 `chat()`；想给终端上色得再 import 一堆。

第 2 章把这三件事一起解决：拆成 `config.py`（所有可调项）+ `client.py`（所有协议细节）+ `main.py`（只负责输入输出）。

---

## 五、踩过的坑

| 现象 | 原因 | 解决 |
|---|---|---|
| `HTTP Error 404`，body 里 `model not found` | 模型名拼错，或没 pull | `ollama list` 看准确名字，注意 `:latest` 后缀 |
| `Connection refused` | Ollama 没启动 | `ollama serve`；Windows 装完默认自启，但可能被安全软件拦了 |
| 第一次请求等 1–2 分钟 | 模型加载进显存 | 正常。第 2 章有 `warmup()` 预热 |
| 公司网络下 `502 Bad Gateway` | `HTTP_PROXY` 环境变量把 localhost 也代理了 | 设 `NO_PROXY=localhost,127.0.0.1`，或代码里用 `urllib.request.build_opener(urllib.request.ProxyHandler({}))` |
| 中文全变 `\u4f60` | 漏了 `ensure_ascii=False` | 加上；这只影响可读性，不影响结果 |
| Ctrl+C 之后模型还在跑（风扇狂转） | 连接没关，Ollama 不知道你走了 | `with` 保证关闭；第 2 章用 `resp.close()` 主动断 |
| `total_duration` 数值巨大 | 单位是纳秒 | 除以 `1e9` |

---

## 六、过关自测

1. 为什么每次请求都要把**整段** `messages` 发过去？模型不是应该有记忆吗？
2. `stream: false` 时，`urllib` 的 `resp.read()` 要等多久才返回？
3. 把 `MODEL` 改成不存在的名字，你看到的报错是哪一行抛的？错误信息里有没有提到模型名？为什么？（这题的答案是第 2 章 `_error_text()` 存在的理由）
4. 如果用户传的 host 是 `http://192.168.1.10:11434`（局域网另一台机器），需要改什么？

<details>
<summary>答案要点</summary>

1. 模型无状态，服务端也不存会话（`/api/chat` 是无状态的，`/api/generate` 也是）。所谓"记忆"完全是客户端把历史带上去造成的错觉。
2. 等到**整段生成结束**才返回。这是 `stream: true` 存在的唯一理由。
3. 抛在 `urlopen` 那一行，`HTTPError`；`str(e)` 只有 `HTTP Error 404: Not Found`，**不含模型名** —— 模型名在响应体里，`HTTPError` 是文件对象，要 `.read()` 才拿得到。不读就永远看不到真正原因。
4. 改 `HOST` 即可；但注意 Ollama 默认只监听 `127.0.0.1`，局域网访问要设 `OLLAMA_HOST=0.0.0.0` 再重启服务。

</details>

---

下一章：把 35 行拆成三个文件，并让文字流出来 → [第 2 章](02-streaming-and-think.md)
