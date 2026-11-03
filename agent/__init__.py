"""agent — 极简本地 Ollama Agent。"""

__version__ = "0.2.0"

APP_NAME = "ollama-agent"

# 应用数据目录名（工作区/主目录下都以它为根）：.ollama-agent。
# 所有隐藏目录的「点 + 名字」一律从这派生，不许再写 ".ollama-agent"、
# ".uploads" 这类字面量——改名/换应用名时只动这一处。
HIDDEN_DIRNAME = f".{APP_NAME}"


def user_agent() -> str:
    """出站 HTTP 请求统一用的 User-Agent，名称与版本只在这里定义一次。

    为什么必须有：urllib 默认发的是 "Python-urllib/3.x"，Cloudflare 之类的
    防护会按客户端特征直接 403（Error 1010: Access denied），而且报错体里
    写着 banned by browser's signature —— 看起来像鉴权问题，实则是 UA 被封。
    实测某中转站，同一个 Key 同一个地址：
        User-Agent: Python-urllib/3.13   → HTTP 403 error code: 1010
        User-Agent: ollama-agent/0.2.0   → HTTP 200
    所以请求里不许写死 UA 字面量，一律引用本函数，改版本只改 __version__。
    """
    return f"{APP_NAME}/{__version__}"
