"""模型连通性探测：新建自定义模型前先验一次，别等开口说话才发现配错。

设计原则（错误文案是产品的一部分）：
    三种失败必须分开报，因为下一步动作完全不同 ——
      连不上       → 查网络/代理/地址/服务是否启动
      401/403     → 换 Key
      200 但没这个模型 → 改模型名，或者从列表里挑

    「连通性失败」四个字对用户毫无用处，他只会去改地址，然后依然不通。

不发真实对话请求：一次 chat 可能几毛钱、也可能因为 prompt 太长超时。
    列模型（GET /v1/models 或 /api/tags）已经能同时验证「地址可达 + Key 有效」，
    另外对 OpenAI 兼容站额外探一次 /v1/models 拿模型清单。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

PROBE_TIMEOUT = 12          # 秒；探测要快，不能让人对着转圈等到超时上限 1800 秒


def _headers(provider: str, api_key: str) -> dict:
    h = {"Accept": "application/json"}
    if provider == "openai" and api_key:
        h["Authorization"] = f"Bearer {api_key}"
    return h


def _models_url(provider: str, host: str) -> str:
    from agent.providers import endpoint
    return endpoint(host, provider, "models")


def probe(provider: str, host: str, api_key: str = "",
          model: str = "") -> dict:
    """探测一个后端配置是否可用。

    返回：
      {"ok": True,  "models": [...], "checked": "models", "note": "..."}
      {"ok": False, "error": "...", "stage": "url|connect|auth|http|parse",
       "hint": "下一步该做什么"}
    永远不抛异常：调用方是 HTTP 处理线程，抛出去会变成 500。
    """
    provider = provider if provider in ("ollama", "openai") else "ollama"
    host = (host or "").strip()
    if not host:
        return {"ok": False, "stage": "url",
                "error": "服务地址是空的",
                "hint": "填完整地址，例如 https://api.deepseek.com/v1 或 http://localhost:11434"}
    if not host.startswith(("http://", "https://")):
        return {"ok": False, "stage": "url",
                "error": f"地址缺少 http:// 或 https:// 前缀（当前：{host}）",
                "hint": "改成 https://… 或 http://… 再测一次"}
    if provider == "openai" and not api_key:
        return {"ok": False, "stage": "auth",
                "error": "OpenAI 兼容协议需要 API Key，现在没填",
                "hint": "到中转站后台复制 Key 粘进来；确实不需要鉴权的本地服务可填任意占位串"}

    url = _models_url(provider, host)
    req = urllib.request.Request(url, headers=_headers(provider, api_key))
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        detail = _error_detail(body)
        if e.code in (401, 403):
            return {"ok": False, "stage": "auth",
                    "error": f"鉴权失败（HTTP {e.code}）：{detail}",
                    "hint": "Key 不对或已过期。注意 Key 区分环境（测试/正式），"
                            "并且不要带 Bearer 前缀，只粘 Key 本身"}
        if e.code == 404:
            return {"ok": False, "stage": "http",
                    "error": f"地址可达但路径不存在（HTTP 404）：{url}",
                    "hint": "OpenAI 兼容站要把地址指到 /v1（例如 …/v1），"
                            "Ollama 原生协议则不要带 /v1"}
        if e.code in (502, 503, 504) and _is_loopback(host):
            # 本机地址收到 502/503/504，几乎都不是「对方服务坏了」：
            # 要么本地服务没起，要么系统代理把 127.0.0.1 也拦了（返回自己的 502 页）。
            # 不点明的话用户会去问「你们站点怎么挂了」，方向全错。
            return {"ok": False, "stage": "connect",
                    "error": f"本机地址 {host} 没有服务在跑（HTTP {e.code} 多来自代理拦截）：{detail}",
                    "hint": "先确认本地服务已启动（例如 ollama serve）；"
                            "如果系统设了 HTTP 代理，把 localhost/127.0.0.1 加进不走代理的名单"}
        return {"ok": False, "stage": "http",
                "error": f"服务返回 HTTP {e.code}：{detail}",
                "hint": "看服务方文档确认这个地址是否支持列模型接口"}
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        # 走系统代理时，连不上本机会被代理包成「Tunnel connection failed: 502」之类的
        # URLError（不是 HTTPError）。这时如果照「境外站点常需代理」去解释，
        # 用户会去调网络，而真实原因是本地服务没起 —— 必须单独指出来。
        if _is_loopback(host):
            return {"ok": False, "stage": "connect",
                    "error": f"连不上本机 {host}：{reason}",
                    "hint": "先确认本地服务已启动（例如 ollama serve，或看端口是否被占用）；"
                            "如果系统设了 HTTP/HTTPS 代理，把 localhost 和 127.0.0.1 "
                            "加进「不走代理」名单，代理会拦本机请求"}
        return {"ok": False, "stage": "connect",
                "error": f"连不上 {host}：{reason}",
                "hint": "确认服务已启动、地址端口没写错；"
                        "访问境外站点常需要代理，先确认浏览器能不能打开它"}
    except TimeoutError:
        return {"ok": False, "stage": "connect",
                "error": f"{PROBE_TIMEOUT} 秒内没有响应：{host}",
                "hint": "地址能解析但连不通。检查端口、防火墙，或换一个可达的镜像站"}
    except OSError as e:
        return {"ok": False, "stage": "connect",
                "error": f"网络错误：{e}",
                "hint": "先确认网络与代理设置，再重试"}

    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        snippet = raw.decode("utf-8", "replace")[:160]
        return {"ok": False, "stage": "parse",
                "error": f"返回的不是 JSON：{snippet!r}",
                "hint": "这个地址多半不是模型服务接口。"
                        "确认没把控制台页面地址（带 #/ 或多层路径）当成 API 地址"}

    models = _parse_models(provider, data)
    out: dict = {"ok": True, "models": models, "checked": "models"}
    if model:
        if model in models:
            out["note"] = f"已确认模型可用：{model}"
        elif models:
            # 不判失败：列模型接口不保证列全（有的中转站只暴露部分）。
            # 给个提示让人自己判断，别把能用的配置拦在门外。
            out["note"] = (f"连通正常，但模型列表里没有「{model}」。"
                           f"该站列出了 {len(models)} 个模型，"
                           f"确认名字拼写是否一致（有些站要求带厂商前缀）")
            out["model_missing"] = True
        else:
            out["note"] = "连通正常，但该站没有返回任何模型；模型名请自行确认"
    return out


def _is_loopback(host: str) -> bool:
    """判断地址是否指向本机（用来区分「对方站挂了」和「我这没起服务」）。"""
    try:
        name = urllib.parse.urlsplit(host).hostname or ""
    except ValueError:
        return False
    name = name.lower()
    return name in ("localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]")


def _parse_models(provider: str, data) -> list[str]:
    if not isinstance(data, dict):
        return []
    if provider == "ollama":
        rows = data.get("models") or []
        return [str(m.get("name") or m.get("model") or "") for m in rows
                if isinstance(m, dict)][:200]
    rows = data.get("data") or []
    if not isinstance(rows, list):
        return []
    return [str(m.get("id") or "") for m in rows if isinstance(m, dict)][:200]


def _error_detail(body: str) -> str:
    """从中转站/OpenAI 的错误体里抠出人能看的那句。"""
    from agent.backends.base import error_text
    return error_text(body)[:200] if body else "（没有返回内容）"
