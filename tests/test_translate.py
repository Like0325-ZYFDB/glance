# -*- coding: utf-8 -*-
"""翻译调度测试：用假来源，不联网。"""
import threading
import time

from glance import translate as tr


def _ok(text):
    return "好的结果"


def _setup():
    tr._DOWN.clear()
    tr._CACHE.clear()
    tr._WARM["at"] = 0.0
    tr.configure("", "")


def test_fallback_waits_for_primary_budget():
    _setup()
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def unavailable(text):
        started.set()
        release.wait(3)
        finished.set()
        raise TimeoutError

    def fallback(text):
        assert started.is_set() and not finished.is_set()
        return _ok(text)

    srcs = [("谷歌翻译", unavailable), ("MyMemory", fallback)]
    try:
        assert tr.translate("hello", srcs, deadline=1.0) == "好的结果"
    finally:
        release.set()
    assert tr.last_source() == "MyMemory"


def test_prefers_higher_priority_within_grace():
    _setup()

    def fast_low(text):
        return "兜底结果"

    def a_bit_slower_high(text):
        time.sleep(0.15)
        return "优先结果"

    assert tr.translate("hello", [("必应翻译", a_bit_slower_high), ("谷歌翻译", fast_low)]) == "优先结果"
    assert tr.last_source() == "必应翻译"


def test_primary_avoids_fallback_and_fallback_is_not_cached(monkeypatch):
    _setup()
    primary, fallback = [], []

    def fast_fallback(text):
        fallback.append(text)
        return "兜底结果"

    def google(text):
        primary.append(text)
        return "优先结果" if len(primary) > 1 else "ENGLISH RESPONSE"

    monkeypatch.setattr(tr, "SOURCES", [("谷歌翻译", google), ("MyMemory", fast_fallback)])
    assert tr.translate("hello") == "兜底结果"
    assert tr.translate("hello") == "优先结果"
    assert tr.translate("hello") == "优先结果"
    assert primary == ["hello", "hello"] and fallback == ["hello"]


def test_all_fail_returns_none():
    _setup()
    assert tr.translate("hello", [("MyMemory", lambda t: t)]) is None   # 原样返回算失败


def test_baidu_skipped_without_key():
    _setup()
    called = []
    srcs = [("百度翻译", lambda t: called.append(1) or "x"), ("MyMemory", _ok)]
    assert tr.translate("hello", srcs) == "好的结果" and not called


def test_cache_and_thread_local_source(monkeypatch):
    _setup()
    calls = []

    def src(text):
        calls.append(text)
        return "译文"

    monkeypatch.setattr(tr, "SOURCES", [("必应翻译", src)])
    assert tr.translate("cache me") == "译文" and tr.last_source() == "必应翻译"
    assert tr.translate("cache me") == "译文" and calls == ["cache me"]   # 第二次走缓存
    seen = []
    t = threading.Thread(target=lambda: seen.append(tr.last_source()))
    t.start()
    t.join()
    assert seen == [""]                     # 别的线程看不到这个线程的来源


class _Resp:
    status_code = 200

    def __init__(self, text="", url="https://www.bing.com/translator", data=None):
        self.text, self.url, self._data = text, url, data

    def json(self):
        if self._data is None:
            raise ValueError("empty")
        return self._data


PAGE = ('<div id="rich_tta" data-iid="translator.5028"></div><script>IG:"ABC123",'
        'var params_AbusePreventionHelper = [1700000000000,"TOKEN1",3600000];</script>')


def test_bing_parses_page_and_refreshes_on_failure(monkeypatch):
    _setup()
    monkeypatch.setattr(tr, "_BING", {"until": 0.0})
    pages, posts = [], []

    class FakeSession:
        def get(self, url, timeout=None):
            pages.append(url)
            return _Resp(PAGE)

        def post(self, url, params=None, data=None, headers=None, timeout=None):
            posts.append((url, params, data))
            if len(posts) == 1:
                return _Resp("")    # 第一次被拒（参数过期）：应重新取参数再试
            return _Resp(data=[{"translations": [{"text": "你好，世界。"}]}])

    monkeypatch.setattr(tr, "_SESSION", FakeSession())
    assert tr._bing("Hello, world.") == "你好，世界。"
    assert len(pages) == 2                   # 第一次取参数 + 被拒后重新取
    url, params, data = posts[-1]
    assert url == "https://www.bing.com/ttranslatev3" and params["IG"] == "ABC123"
    assert data["token"] == "TOKEN1" and data["key"] == "1700000000000" and data["to"] == "zh-Hans"
    assert params["IID"].startswith("translator.5028.")
    tr._bing("你好")                         # 中文翻成英文
    assert posts[-1][2]["to"] == "en"


def test_warm_is_rate_limited(monkeypatch):
    _setup()
    warmed = []
    monkeypatch.setattr(tr, "_warm_one", lambda name: warmed.append(name))
    tr.warm()
    tr.warm()                                # 30 秒内第二次不再预热
    time.sleep(0.2)
    assert sorted(warmed) == sorted(["必应翻译", "谷歌翻译"])


def test_chunks():
    text = "This is one sentence. " * 60
    parts = tr._chunks(text)
    assert all(len(p) <= 450 for p in parts) and len(parts) >= 2
    assert "".join(parts) == text
    text = "这是一段中文。\n\n" * 240
    parts = tr._chunks(text, size=500, utf8=True)
    assert len(parts) > 6 and all(len(part.encode("utf-8")) <= 500 for part in parts)
    assert "".join(parts) == text


def test_mixed_language_direction_and_wrong_language_results():
    _setup()
    assert not tr._is_chinese("Keep the label 中文 in this sentence.")
    assert tr._is_chinese("使用 GitHub 提交这次更新。")
    assert not tr._is_chinese("翻訳をお願いします。")
    assert tr.translate("这段是中文。", [("必应翻译", lambda t: "另一段中文。")]) is None
    assert tr.translate("这段是中文。", [("谷歌翻译", lambda t: "This is Chinese.")]) == "This is Chinese."


def test_baidu_translates_beyond_first_chunk(monkeypatch):
    _setup()
    tr.configure("test-app", "test-key")
    calls = []

    class FakeSession:
        def post(self, url, data, timeout):
            calls.append(data)
            return _Resp(data={"trans_result": [{"dst": f"译文{len(calls)}"}]})

    monkeypatch.setattr(tr, "_SESSION", FakeSession())
    text = "Read this whole sentence. " * 110 + "End."
    assert tr._baidu(text) == "译文1译文2"
    assert len(calls) == 2 and calls[-1]["q"].endswith("End.")
    assert {call["to"] for call in calls} == {"zh"}


def test_mymemory_rejects_quota_response_even_when_http_succeeds(monkeypatch):
    _setup()

    class FakeSession:
        def get(self, url, params, timeout):
            return _Resp(data={"responseStatus": 429, "responseData": {"translatedText": "额度已用完"}})

    monkeypatch.setattr(tr, "_SESSION", FakeSession())
    assert tr._mymemory("A sample sentence.") is None


def test_short_text_keeps_what_the_top_source_says(monkeypatch):
    """专有名词：必应原样返回是对的，不换成别家的硬译，也不去问兜底来源；结果记进缓存。"""
    _setup()
    calls = []

    def bing(text):
        calls.append("必应")
        return text

    def google(text):
        calls.append("谷歌")
        return "聊天GPT"

    def mymemory(text):
        calls.append("MyMemory")
        return "不相干的一句话"

    monkeypatch.setattr(tr, "SOURCES", [("必应翻译", bing), ("谷歌翻译", google), ("MyMemory", mymemory)])
    assert tr.translate("ChatGPT") is None
    assert tr.last_status() == "unchanged" and tr.last_source() == "必应翻译"
    assert "MyMemory" not in calls
    before = len(calls)
    assert tr.translate("ChatGPT") is None and tr.last_status() == "unchanged"
    assert len(calls) == before              # 第二次走缓存，不再联网


def test_short_text_uses_lower_source_when_top_one_is_unreachable():
    _setup()

    def down(text):
        raise TimeoutError

    assert tr.translate("serendipity", [("必应翻译", down), ("谷歌翻译", lambda t: "意外之喜")]) == "意外之喜"
    assert tr.last_status() == "ok" and tr.last_source() == "谷歌翻译"


def test_sentence_prefers_a_real_translation_over_an_echo():
    """整句话被原样返回多半是那个来源没翻：有别的来源给了译文就用译文。"""
    _setup()
    text = "This sentence is long enough to count as a sentence."
    srcs = [("必应翻译", lambda t: t), ("谷歌翻译", lambda t: "这句话足够长，算得上一个句子。")]
    assert tr.translate(text, srcs) == "这句话足够长，算得上一个句子。"
    assert tr.last_source() == "谷歌翻译" and tr.last_status() == "ok"
    assert tr.translate(text, [("必应翻译", lambda t: t)]) is None and tr.last_status() == "unchanged"


def test_fallback_only_when_no_primary_answers():
    _setup()
    called = []

    def mymemory(text):
        called.append(text)
        return "兜底结果"

    def down(text):
        raise TimeoutError

    assert tr.translate("xyzzy", [("必应翻译", lambda t: t), ("MyMemory", mymemory)]) is None
    assert not called and tr.last_status() == "unchanged"
    assert tr.translate("hello", [("必应翻译", down), ("MyMemory", mymemory)]) == "兜底结果"
    assert tr.translate("hello", [("必应翻译", down), ("MyMemory", lambda t: t)]) is None
    assert tr.last_status() == "failed"      # 兜底来源原样返回不可信


def test_direction_of_mixed_text():
    _setup()
    # 夹着命令、术语的中文：翻成英文
    assert tr._is_chinese("运行 npm install 然后 npm run dev")
    assert tr._is_chinese("使用 git rebase -i HEAD~3 修改 commit message")
    assert tr._is_chinese("中文里夹着一个の字的句子，应该还是中文。")
    # 引了中文词的英文句子、日文：翻成中文
    assert not tr._is_chinese("The words 你好 and 谢谢 are common greetings.")
    assert not tr._is_chinese("東京都渋谷区の天気")
    assert not tr._is_chinese("の")
    # 方向说不准的原文，来源往哪边翻都接受
    mixed = "运行 npm install 然后 npm run dev"
    assert tr.translate(mixed, [("必应翻译", lambda t: "Run npm install and then npm run dev")])
    assert tr.translate(mixed, [("必应翻译", lambda t: "运行 npm 安装，然后运行 npm 开发")])


def test_google_splits_long_chinese(monkeypatch):
    """谷歌端点把原文放在网址里：3000 个汉字要分几次请求，拼回来不丢内容。"""
    _setup()
    asked = []

    class FakeSession:
        def get(self, url, params, timeout):
            asked.append(params["q"])
            return _Resp(data=[[[f"<{params['q']}>", params["q"]]]])

    monkeypatch.setattr(tr, "_SESSION", FakeSession())
    text = "这是一句用来凑长度的中文。" * 230
    out = tr._google(text)
    assert len(asked) >= 3 and all(len(q.encode("utf-8")) <= tr.GOOGLE_LIMIT for q in asked)
    assert out.replace("<", "").replace(">", "").replace(" ", "") == text


def test_parallel_chunks_keep_order():
    def slow_first(chunk):
        time.sleep(0.2 if chunk == "one" else 0)
        return chunk.upper()

    out = tr._translate_chunks(["one ", "two ", "three"], slow_first, True, parallel=True)
    assert out == "ONE TWO THREE"
    assert tr._translate_chunks(["one ", "two "], lambda c: None if c == "two" else c, True, parallel=True) is None


def test_long_text_gets_more_time(monkeypatch):
    """长原文多给时间，而且兜底来源接不了的长度不给它留时间。"""
    _setup()
    text = "Read this whole sentence. " * 110

    def slow(t):
        time.sleep(0.6)
        return "译文"

    monkeypatch.setattr(tr, "DEADLINE", 0.4)    # 短原文只等 0.4 秒；这段有 2800 多字，另加 9 秒多
    monkeypatch.setattr(tr, "SOURCES", [("必应翻译", slow), ("MyMemory", lambda t: "兜底")])
    assert tr.translate(text) == "译文" and tr.last_source() == "必应翻译"
    assert tr.translate("hello") == "兜底"
