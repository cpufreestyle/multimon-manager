"""窗口标题的宽松匹配（复刻 Rectangle Pro 的 lenientMatch / TitleMatchingMode）。

精确匹配 "app::title" 在标题经常变化的 App 上很容易漏：ChatGPT 的标题在
"ChatGPT" / "ChatGPT — New Chat" / "ChatGPT — 某个会话名" 之间来回跳，
布局保存时记的名字还原时早就不存在，那一格就被判成「未找到」——用户看到的
就是「布局还原少了一个窗口」。

匹配强度分三档，从强到弱：
   1. exact      完整相等（大小写不敏感）
   2. contained   互为子串（"chatgpt" 在 "chatgpt — new chat" 里）
   3. word        词集合有交集（按非字母数字切词，交集非空即算匹配）

第 3 档是 Rectangle Pro 的 lenientMatch 思路：标题里的实词（应用名、文件名）
通常不变，变的是连接词和会话名。返回 (mode, score)，score 供同级候选排序。
"""


def _words(s):
    """把标题切成小写词集合（按非字母数字切，丢掉单字符词）。"""
    out = set()
    cur = []
    for ch in (s or "").lower():
        if ch.isalnum():
            cur.append(ch)
        elif cur:
            w = "".join(cur)
            if len(w) > 1:
                out.add(w)
            cur = []
    if cur:
        w = "".join(cur)
        if len(w) > 1:
            out.add(w)
    return out


def match_strength(saved_title, cur_title):
    """返回 (mode, score)；mode 为 None 表示不匹配。

    score 越大越像：exact=100，contained 按覆盖比例 50~99，word 按交集
    占保存标题词数的比例 1~49。调用方在多个候选里取 score 最大的。
    """
    a = (saved_title or "").strip().lower()
    b = (cur_title or "").strip().lower()
    if not a or not b:
        return (None, 0)
    if a == b:
        return ("exact", 100)
    # 互为子串：短的一方被长的一方完整包含
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if short in long_:
        # 覆盖比例越高越像；留出 50~99 区间，低于 exact
        score = 50 + int(round(49.0 * len(short) / len(long_)))
        return ("contained", score)
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return (None, 0)
    inter = wa & wb
    if not inter:
        return (None, 0)
    # 交集占保存标题词数的比例；1~49 区间，弱于 contained
    score = 1 + int(round(48.0 * len(inter) / len(wa)))
    return ("word", score)


def best_candidate(saved_title, candidates):
    """在 [(hwnd, title), ...] 里找最像 saved_title 的候选。

    返回 (hwnd, mode, score)；没有够格的候选返回 (None, None, 0)。
    至少要求 word 档（词集合有交集），纯拼写巧合不算。
    """
    best = (None, None, 0)
    for hwnd, title in candidates:
        mode, score = match_strength(saved_title, title)
        if mode is None or score < 1:
            continue
        if score > best[2]:
            best = (hwnd, mode, score)
    return best
