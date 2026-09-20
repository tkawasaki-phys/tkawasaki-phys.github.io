#!/usr/bin/env python3
"""
ORCID を唯一の名簿として業績を集め、index.html と publications.html を生成する。

    python3 tools/sync.py            # 取得して再生成
    python3 tools/sync.py --offline  # 取得せず、キャッシュから再生成だけ

設計上の約束ごと:

  * 業績は「ORCID に本人が登録した DOI」だけを名簿とする。著者名でも
    OpenAlex の著者 ID でも検索しない。OpenAlex の著者エンドポイントは
    同姓イニシャルの別人を統合してしまい、実測で 80 件中 51 件が別人だった。
    DOI を鍵にして引けば、この事故は構造的に起こらない。

  * 取得に失敗しても既存の data/publications.json を壊さない。
    1 件でも取れなかったらキャッシュの値をそのまま使う。

  * 業績は HTML に焼き込む。実行時に JavaScript で取りに行くと、
    検索エンジンに拾われず、API が落ちた日に業績欄が空白になる。

標準ライブラリだけで動く。追加インストールは要らない。
"""

import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, timezone, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
TPL = os.path.join(ROOT, "templates")
CACHE = os.path.join(DATA, "publications.json")
RM_CACHE = os.path.join(DATA, "researchmap.json")

# researchmap から引いてくる欄。論文は ORCID を正とするので、ここには入れない。
RM_SECTIONS = ("research_experience", "education", "research_projects",
               "industrial_property_rights", "presentations", "teaching_experience")

OFFLINE = "--offline" in sys.argv


# ---------------------------------------------------------------- HTTP

def http_json(url, accept="application/json", tries=3):
    """urllib で取り、証明書が無い環境（MacPorts python など）では curl に落ちる。"""
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": UA})
    last = None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.load(r)
        except Exception as exc:                      # noqa: BLE001
            last = exc
            if "CERTIFICATE_VERIFY_FAILED" in repr(exc):
                break
            time.sleep(1.5 * (attempt + 1))
    try:
        out = subprocess.run(
            ["curl", "-sS", "--fail", "--retry", "2", "-A", UA, "-H", "Accept: " + accept, url],
            capture_output=True, text=True, timeout=90,
        )
        if out.returncode == 0 and out.stdout.strip():
            return json.loads(out.stdout)
        last = RuntimeError("curl exit %s: %s" % (out.returncode, out.stderr.strip()[:200]))
    except Exception as exc:                          # noqa: BLE001
        last = exc
    raise RuntimeError("fetch failed: %s\n  %r" % (url, last))


# ---------------------------------------------------------------- 取得

def researchmap(permalink):
    """経歴・学歴・科研費・特許を researchmap から引く。

    ここは業績（論文）には一切使わない。論文の名簿は ORCID だけが持つ、という
    この道具の前提を崩さないため。researchmap にしか無い情報だけを取りに行く。
    """
    out = {}
    # 学位は /education ではなく、プロフィール本体の degrees に入っている。
    try:
        root = http_json("https://api.researchmap.jp/%s" % permalink) or {}
        out["_profile"] = {"degrees": root.get("degrees") or []}
    except Exception as exc:                           # noqa: BLE001
        print("  ! researchmap のプロフィール本体が取れませんでした: %s" % str(exc)[:70])
    for sec in RM_SECTIONS:
        url = "https://api.researchmap.jp/%s/%s" % (permalink, sec)
        try:
            out[sec] = (http_json(url) or {}).get("items", [])
        except Exception as exc:                       # noqa: BLE001
            print("  ! researchmap %s が取れませんでした: %s" % (sec, str(exc)[:70]))
        time.sleep(0.1)
    return out


def orcid_dois(orcid):
    """ORCID の業績一覧から DOI と arXiv ID を取り出す。ここが唯一の名簿。"""
    rec = http_json("https://pub.orcid.org/v3.0/%s/record" % orcid)
    works = ((rec.get("activities-summary") or {}).get("works") or {}).get("group") or []
    out, skipped = [], []
    for g in works:
        ids = {}
        for e in (g.get("external-ids") or {}).get("external-id", []):
            t = (e.get("external-id-type") or "").lower()
            v = (e.get("external-id-value") or "").strip()
            if t and v and t not in ids:
                ids[t] = v
        summary = (g.get("work-summary") or [{}])[0]
        title = (((summary.get("title") or {}).get("title") or {}).get("value") or "").strip()
        if "doi" in ids:
            arx = ids.get("arxiv", "")
            arx = re.sub(r"^arxiv:", "", arx, flags=re.I).split("v")[0] if arx else ""
            out.append({"doi": ids["doi"].lower().strip(), "arxiv": arx})
        else:
            skipped.append(title or "(no title)")
    if skipped:
        print("  ! DOI が無いため載せられない ORCID の項目 %d 件:" % len(skipped))
        for t in skipped:
            print("      -", t[:78])
    return out


def crossref(doi):
    j = http_json("https://api.crossref.org/works/%s?mailto=%s"
                  % (urllib.parse.quote(doi), urllib.parse.quote(MAILTO)))
    m = j.get("message") or {}
    authors = []
    for a in m.get("author") or []:
        given = (a.get("given") or "").strip()
        family = (a.get("family") or "").strip()
        if not family and not given:
            continue                                   # 共同研究名義の空エントリ
        initials = "".join(p[0] + "." for p in re.split(r"[\s-]+", given) if p)
        authors.append({"given": given, "family": family,
                        "short": (initials + " " + family).strip()})
    n_authors = len(authors)
    position = None
    want_f = (PROFILE["author_match"]["family"] or "").lower()
    want_g = (PROFILE["author_match"]["given_prefix"] or "").lower()
    for i, a in enumerate(authors):
        if a["family"].lower() == want_f and a["given"].lower().startswith(want_g):
            position = i + 1
            break

    # 著者が多い論文の全リストは出さないので捨てる。残すと publications.json が
    # 数 MB になり、毎週の自動コミットがその差分で埋まる。著者順は上で確定済み。
    if n_authors >= ABBREV_MIN:
        authors = authors[:3]

    page = (m.get("page") or m.get("article-number") or "").strip()
    page = re.sub(r"(\d)-(\d)", "\\1–\\2", page)       # 35-40 → 35–40 （en dash）

    issued = (m.get("issued") or {}).get("date-parts") or [[None]]
    return {
        "doi": doi,
        "title": clean(m.get("title") or [""]),
        "container": clean(m.get("short-container-title") or []) or clean(m.get("container-title") or []),
        "year": issued[0][0],
        "volume": (m.get("volume") or "").strip(),
        "page": page,
        "authors": authors,
        "n_authors": n_authors,
        "position": position,
    }


def openalex_citations(dois):
    """DOI を 40 件ずつまとめて被引用数だけ引く。著者では引かない。"""
    got = {}
    for i in range(0, len(dois), 40):
        chunk = dois[i:i + 40]
        filt = "doi:" + "|".join("https://doi.org/" + d for d in chunk)
        url = ("https://api.openalex.org/works?filter=%s&per-page=50&mailto=%s"
               % (urllib.parse.quote(filt, safe=":|/."), urllib.parse.quote(MAILTO)))
        for w in (http_json(url).get("results") or []):
            d = (w.get("doi") or "").replace("https://doi.org/", "").lower()
            if d:
                got[d] = w.get("cited_by_count") or 0
        time.sleep(0.3)
    return got


def clean(values):
    """Crossref のタイトルには <i> や実体参照が混ざる。素のテキストに均す。"""
    s = values[0] if values else ""
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    # Crossref のタイトルには "Overview of KAGRA : ..." のようにコロンの前へ
    # 空白が入っているものがある。出版社側の入力揺れなので、ここで整える。
    return re.sub(r"\s+([:;,])", "\\1", s)


# ---------------------------------------------------------------- 組み立て

def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def bilingual(tag, en, ja, cls=""):
    """英語を素で書き、日本語は属性に持たせる。JS 無効でも英語が読める。"""
    c = ' class="%s"' % cls if cls else ""
    return '<%s%s data-en="%s" data-ja="%s">%s</%s>' % (tag, c, esc(en), esc(ja), esc(en), tag)


def ordinal(n):
    if 10 <= n % 100 <= 20:
        return "%dth" % n
    return "%d%s" % (n, {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th"))


def author_position(pub):
    if pub.get("position"):
        return pub["position"]
    want_f = (PROFILE["author_match"]["family"] or "").lower()
    want_g = (PROFILE["author_match"]["given_prefix"] or "").lower()
    for i, a in enumerate(pub.get("authors") or []):
        if a["family"].lower() == want_f and a["given"].lower().startswith(want_g):
            return i + 1
    return None


def citation_html(pub, labels):
    """1 件を台帳の 1 行に組む。"""
    n = pub.get("n_authors") or 0
    manual = labels.get(pub["doi"])
    pos = author_position(pub)

    def names(seq):
        out = []
        for a in seq:
            t = esc(a["short"])
            if a["family"].lower() == (PROFILE["author_match"]["family"] or "").lower() \
               and a["given"].lower().startswith((PROFILE["author_match"]["given_prefix"] or "").lower()):
                t = '<span class="me">%s</span>' % t
            out.append(t)
        return ", ".join(out)

    if n >= COLLAB_MIN:
        # 千人規模の共同研究。著者順はアルファベット順で意味を持たないので出さない。
        who = esc(manual) if manual else ""
        badge = '<span class="collab">collaboration &middot; %s authors</span>' % f"{n:,}"
    elif n >= ABBREV_MIN:
        # 数十人規模。全員は並べないが、本人の立ち位置は分かるようにする。
        shown = pub.get("authors") or []
        who = esc(manual) if manual else (names(shown) + (" <i>et al.</i>" if len(shown) < n else ""))
        if pos and pos > len(shown):
            who += ' (incl. <span class="me">%s</span>)' % esc(SELF_SHORT)
        badge = ('<span class="pos">%s of %d</span>' % (ordinal(pos), n)) if pos else \
                ('<span class="collab">%d authors</span>' % n)
    else:
        who = names(pub.get("authors") or [])
        badge = ('<span class="pos">%s of %d</span>' % (ordinal(pos), n)) if pos else ""

    name = VENUE_OVERRIDES.get(pub["doi"]) or VENUE_OVERRIDES.get(pub.get("container") or "") \
        or pub.get("container")
    venue = ""
    if name:
        venue = "<em>%s</em>" % esc(name)
        if pub.get("volume"):
            venue += " %s" % esc(pub["volume"])
        if pub.get("page"):
            venue += ", %s" % esc(pub["page"])
    meta = " &mdash; ".join(x for x in (who, venue) if x)

    tail = [badge] if badge else []
    tail.append('<a href="https://doi.org/%s" rel="noopener">doi</a>' % esc(pub["doi"]))
    if pub.get("arxiv"):
        tail.append('<a href="https://arxiv.org/abs/%s" rel="noopener">arXiv:%s</a>'
                    % (esc(pub["arxiv"]), esc(pub["arxiv"])))
    if pub.get("cited_by"):
        tail.append('<span class="cited">cited <b>%s</b></span>' % f"{pub['cited_by']:,}")

    return (
        '<li class="pub">\n'
        '  <div class="pub-year">%s</div>\n'
        '  <div>\n'
        '    <h3 class="pub-title">%s</h3>\n'
        '    <p class="pub-meta">%s</p>\n'
        '    <div class="pub-tail">%s</div>\n'
        '  </div>\n'
        '</li>' % (esc(pub.get("year") or ""), esc(pub["title"]), meta, "".join(tail))
    )


def seal():
    """サイトマーク。図（seal_svg）があれば図を、無ければ文字（seal）を出す。

    図は色を currentColor で描くので、差し色を変えても追従する。地も図の側で塗る。
    文字に戻したときは CSS で地を塗るので、区別できるよう seal-txt を足す。
    """
    svg = (PROFILE.get("seal_svg") or "").strip()
    if svg:
        return ('<span class="seal" aria-hidden="true">'
                '<svg viewBox="0 0 24 24" fill="currentColor">%s</svg></span>' % svg)
    return '<span class="seal seal-txt" aria-hidden="true">%s</span>' % esc(PROFILE.get("seal", ""))


def bar(active):
    en, ja = PROFILE["en"], PROFILE["ja"]
    le, lj = en["labels"], ja["labels"]
    items = [("research", "index.html#research"), ("publications", "publications.html"),
             ("contact", "index.html#contact")]
    links = []
    for key, href in items:
        cur = ' aria-current="page"' if key == active else ""
        links.append('<a href="%s"%s data-en="%s" data-ja="%s">%s</a>'
                     % (href, cur, esc(le[key]), esc(lj[key]), esc(le[key])))
    return (
        '<header class="bar">\n'
        '  <div class="bar-in">\n'
        '    <a class="mark" href="index.html">%s'
        '<b data-en="%s" data-ja="%s">%s</b></a>\n'
        '    <nav>%s</nav>\n'
        '    <div class="langs" role="group" aria-label="Language">\n'
        '      <button class="langbtn" data-lang="en" aria-pressed="true">EN</button>\n'
        '      <button class="langbtn" data-lang="ja" aria-pressed="false">JA</button>\n'
        '    </div>\n'
        '  </div>\n'
        '</header>' % (seal(), esc(en["name"]), esc(ja["name"]), esc(en["name"]),
                       "".join(links))
    )


def footer(updated):
    en, ja = PROFILE["en"], PROFILE["ja"]
    return (
        '<footer>\n'
        '  <span data-en="%s" data-ja="%s">%s</span>\n'
        '  <span class="sync"><span data-en="%s" data-ja="%s">%s</span> &middot; %s</span>\n'
        '</footer>' % (
            esc("© %d %s" % (date.today().year, en["name"])),
            esc("© %d %s" % (date.today().year, ja["name"])),
            esc("© %d %s" % (date.today().year, en["name"])),
            esc(en["labels"]["synced"]), esc(ja["labels"]["synced"]), esc(en["labels"]["synced"]),
            esc(updated))
    )


def hero():
    en, ja = PROFILE["en"], PROFILE["ja"]
    ids = "".join(
        '<a href="%s" rel="noopener">%s</a>' % (esc(l["url"]), esc(l["label"]))
        for l in PROFILE["links"] if (l.get("url") or "").strip()
    )
    # 写真があればそれを、無ければ頭文字の枠を出す。
    # 撮り直しのあいだファイルを外しても、体裁が崩れないようにしておく。
    photo = os.path.join(ROOT, "assets", "portrait.jpg")
    if os.path.exists(photo):
        portrait = ('  <div class="portrait">\n'
                    '    <img src="assets/portrait.jpg?v=%s" alt="%s" width="640" height="800">\n'
                    '  </div>\n' % (asset_stamp("assets/portrait.jpg"), esc(en["name"])))
    else:
        portrait = ('  <div class="portrait" aria-hidden="true">\n'
                    '    <span class="mono">%s</span>'
                    '<span class="cap" data-en="%s" data-ja="%s">%s</span>\n'
                    '  </div>\n' % (
                        esc(PROFILE["monogram"]),
                        esc(en["labels"]["photo"]), esc(ja["labels"]["photo"]),
                        esc(en["labels"]["photo"])))

    return (
        '<div class="hero">\n'
        '%s'
        '  <div class="hero-txt">\n'
        '    %s\n'
        '    %s\n'
        '    %s\n'
        '    <div class="ids">%s</div>\n'
        '  </div>\n'
        '</div>' % (
            portrait,
            bilingual("h1", en["name"], ja["name"]),
            bilingual("p", "%s · %s" % (en["role"], en["affiliation"]),
                      "%s · %s" % (ja["role"], ja["affiliation"]), "role"),
            bilingual("p", en["statement"], ja["statement"], "statement"),
            ids)
    )


def head(key, side=""):
    en, ja = PROFILE["en"]["labels"], PROFILE["ja"]["labels"]
    return ('<div class="head">%s%s</div>'
            % (bilingual("h2", en[key], ja[key]),
               '<span class="side">%s</span>' % side if side else ""))


def sub_head(key):
    en, ja = PROFILE["en"]["labels"], PROFILE["ja"]["labels"]
    return bilingual("h3", en[key], ja[key], "subhead")


def themes():
    out = []
    for e, j in zip(PROFILE["en"]["themes"], PROFILE["ja"]["themes"]):
        ek, jk = e.get("keys", []), j.get("keys", [])
        if len(jk) != len(ek):                 # 対応が崩れていたら英語で通す
            jk = ek
        keys = "".join(bilingual("span", a, b) for a, b in zip(ek, jk))
        out.append('<article class="theme">%s%s%s</article>'
                   % (bilingual("h3", e["title"], j["title"]),
                      bilingual("p", e["body"], j["body"]),
                      '<div class="keys">%s</div>' % keys if keys else ""))
    return ('<section id="research" class="block">%s<div>%s</div></section>'
            % (head("research"), "".join(out)))


def featured(pubs, sel):
    by_doi = {p["doi"]: p for p in pubs}
    rows, missing = [], []
    for d in sel.get("featured", []):
        p = by_doi.get(d.lower())
        if p:
            rows.append(citation_html(p, sel.get("labels", {})))
        else:
            missing.append(d)
    if missing:
        print("  ! selected.json の DOI が ORCID に無い:", ", ".join(missing))
    en, ja = PROFILE["en"]["labels"], PROFILE["ja"]["labels"]
    side = ('<a href="publications.html" data-en="%s" data-ja="%s">%s</a>'
            % (esc("%s (%d) →" % (en["all"], len(pubs))),
               esc("%s（%d 件）→" % (ja["all"], len(pubs))),
               esc("%s (%d) →" % (en["all"], len(pubs)))))
    return ('<section id="publications" class="block">%s<ol class="ledger">%s</ol></section>'
            % (head("selected", side), "".join(rows)))


def rm_text(node, lang="en"):
    """researchmap は {"ja": ..., "en": ...} で返す。片方しか無い欄も多い。

    英語が空のまま英語ページに日本語が出ると不格好なので、profile.json の
    text_overrides で訳を補える。それも無ければ日本語をそのまま出す。
    """
    if not isinstance(node, dict):
        return (node or "")
    got = node.get(lang)
    if got:
        return got
    ja = node.get("ja") or ""
    if lang == "en" and ja:
        return PROFILE.get("text_overrides", {}).get(ja) or ja
    return ja or node.get("en") or ""


def rm_period(frm, to):
    """"2024-09" と "9999" を "2024.09 – " に均す。9999 は researchmap の「現在」。"""
    fmt = lambda v: (v or "")[:7].replace("-", ".")
    a = fmt(frm)
    if not to or to.startswith("9999"):
        return a, None                     # 継続中。相手側はラベルで出す
    return a, fmt(to)


def rm_rows(items, line1, line2, badge=None):
    """経歴・学歴のような「期間 + 2 行」の台帳を組む。日英どちらも持たせる。"""
    rows = []
    for it in items:
        a, b = rm_period(it.get("from_date"), it.get("to_date"))
        if b is None:
            span = '%s&ndash;<span data-en="%s" data-ja="%s">%s</span>' % (
                esc(a), esc(PROFILE["en"]["labels"]["present"]),
                esc(PROFILE["ja"]["labels"]["present"]), esc(PROFILE["en"]["labels"]["present"]))
        else:
            span = "%s&ndash;%s" % (esc(a), esc(b))
        what = bilingual("p", line1(it, "en"), line1(it, "ja"), "cv-what")
        sub_en, sub_ja = line2(it, "en"), line2(it, "ja")
        sub = bilingual("p", sub_en, sub_ja, "cv-sub") if sub_en or sub_ja else ""
        mark = badge(it) if badge else ""
        rows.append('<li class="cv"><div class="cv-when">%s</div><div>%s%s%s</div></li>'
                    % (span, what, sub, mark))
    return "".join(rows)


def career(rm):
    """職歴と学歴。どちらも researchmap にしか無い。"""
    jobs = rm.get("research_experience") or []
    edu = rm.get("education") or []
    teach = rm.get("teaching_experience") or []
    if not jobs and not edu and not teach:
        return ""

    def job_where(it, lang):
        bits = [rm_text(it.get("affiliation"), lang), rm_text(it.get("section"), lang)]
        return " ".join(x for x in bits if x)

    def job_what(it, lang):
        return rm_text(it.get("job"), lang)

    def edu_where(it, lang):
        return rm_text(it.get("affiliation"), lang)

    def edu_what(it, lang):
        bits = [rm_text(it.get("department"), lang), rm_text(it.get("course"), lang)]
        return " ".join(x for x in bits if x)

    # 学位は researchmap のプロフィール本体（degrees）にあり、/education には無い。
    # 取得年月が一致する在籍期間の行に添える。別行にすると同じ期間が二度並ぶ。
    degrees = (rm.get("_profile") or {}).get("degrees") or []
    by_end = {}
    for g in degrees:
        if g.get("degree_date"):
            by_end.setdefault(g["degree_date"][:7], []).append(g)

    def degree_badge(it):
        got = by_end.get((it.get("to_date") or "")[:7]) or []
        return "".join(
            '<p class="cv-badge"><span class="pos only-en">%s</span>'
            '<span class="pos only-ja">%s</span></p>'
            % (esc(rm_text(g.get("degree"), "en")), esc(rm_text(g.get("degree"), "ja")))
            for g in got)

    leftover = [g for g in degrees
                if (g.get("degree_date") or "")[:7] not in {(e.get("to_date") or "")[:7] for e in edu}]
    if leftover:
        print("  ! 在籍期間と結び付かない学位 %d 件（別行では出していません）" % len(leftover))

    out = []
    if jobs:
        out.append('<div class="cv-group">%s<ol class="cvlist">%s</ol></div>'
                   % (sub_head("career"), rm_rows(jobs, job_where, job_what)))
    if edu:
        out.append('<div class="cv-group">%s<ol class="cvlist">%s</ol></div>'
                   % (sub_head("education"), rm_rows(edu, edu_where, edu_what, degree_badge)))

    # 担当科目は 1 件しか無いので独立した欄は作らず、経歴の中に並べる。
    # 中身の薄い欄を増やすより、職歴・学歴と同じ台帳に置いたほうが締まる。
    if teach:
        def sub_where(it, lang):
            # bilingual() が中身をエスケープするので、実体参照ではなく文字そのものを置く
            sub = rm_text(it.get("subject_name"), lang)
            org = rm_text(it.get("institution_name"), lang)
            if not org:
                return sub
            return "%s（%s）" % (sub, org) if lang == "ja" else "%s \u00b7 %s" % (sub, org)

        def sub_what(it, lang):
            return rm_text(it.get("description"), lang)

        out.append('<div class="cv-group">%s<ol class="cvlist">%s</ol></div>'
                   % (sub_head("teaching"), rm_rows(teach, sub_where, sub_what)))
    return ('<section id="career" class="block">%s<div class="cv-cols">%s</div></section>'
            % (head("career_head"), "".join(out)))


def grant_amount(it):
    """総額を日英で組む。日本語は万円、英語は円のまま桁区切り。

    万円に丸めるのは端数の無い課題だけ。科研費は千円単位の課題もあるので、
    割り切れないものを「約」で丸めると金額を書き換えたことになる。
    """
    raw = (it.get("overall_grant_amount") or {}).get("total_cost")
    try:
        yen = int(raw)
    except (TypeError, ValueError):
        return "", ""
    if yen <= 0:
        return "", ""
    ja = ("%d万円" % (yen // 10000)) if yen % 10000 == 0 else ("%s円" % f"{yen:,}")
    return "JPY %s" % f"{yen:,}", ja


def grants(rm):
    """科研費。役割は researchmap では空欄のことがあるので、無ければ出さない。
    「研究代表者」と書いてよいのは、そう確認できたものだけ。"""
    items = rm.get("research_projects") or []
    if not items:
        return ""
    known = {r["grant_number"]: r for r in PROFILE.get("grant_roles", [])}
    rows = []
    for it in sorted(items, key=lambda x: (x.get("from_date") or ""), reverse=True):
        nums = (it.get("identifiers") or {}).get("grant_number") or []
        num = nums[0] if nums else ""
        # 台帳の左端は年だけにする。年月まで入れると桁があふれて折り返す。
        ya = (it.get("from_date") or "")[:4]
        yb = (it.get("to_date") or "")[:4]
        span = ("%s&ndash;%s" % (esc(ya), esc(yb))) if yb and yb != ya else esc(ya)

        role = it.get("research_project_owner_role") or (known.get(num) or {}).get("role") or ""
        tail = []
        if role == "principal_investigator":
            tail.append('<span class="pos" data-en="Principal Investigator" '
                        'data-ja="研究代表者">Principal Investigator</span>')
        elif role:
            tail.append('<span class="pos">%s</span>' % esc(role.replace("_", " ")))
        if num:
            kaken = "https://kaken.nii.ac.jp/ja/grant/KAKENHI-PROJECT-%s/" % num
            tail.append('<a href="%s" rel="noopener">%s</a>' % (esc(kaken), esc(num)))
        money_en, money_ja = grant_amount(it)
        if money_en:
            tail.append('<span class="cited" data-en="%s" data-ja="%s">%s</span>'
                        % (esc(money_en), esc(money_ja), esc(money_en)))

        rows.append(
            '<li class="pub">\n'
            '  <div class="pub-year">%s</div>\n'
            '  <div>\n'
            '    %s\n'
            '    %s\n'
            '    <div class="pub-tail">%s</div>\n'
            '  </div>\n'
            '</li>' % (
                span,
                bilingual("h3", rm_text(it.get("research_project_title"), "en"),
                          rm_text(it.get("research_project_title"), "ja"), "pub-title"),
                bilingual("p", rm_text(it.get("category"), "en"),
                          rm_text(it.get("category"), "ja"), "pub-meta"),
                "".join(tail)))
    return ('<section id="grants" class="block">%s<ol class="ledger">%s</ol></section>'
            % (head("grants"), "".join(rows)))


def us_patent(see_also):
    """Google Patents の公開番号から、米国での段階を読む。

    末尾の種別コードが答えを持っている。B2 は登録済み、A1 は公開のみ。
    researchmap の説明文を読みに行くより、この番号のほうが崩れない。
    """
    for ref in (see_also or []):
        m = re.search(r"/patent/US(\d+)(B\d)/", ref.get("@id") or "")
        if m:
            n = m.group(1)
            pretty = "US %s,%s,%s %s" % (n[:-6], n[-6:-3], n[-3:], m.group(2))
            return "granted", pretty, ref["@id"]
        m = re.search(r"/patent/US(\d{4})(\d+)(A\d)/", ref.get("@id") or "")
        if m:
            return "published", "US %s/%s %s" % (m.group(1), m.group(2), m.group(3)), ref["@id"]
    return None, "", ""


def patents(rm):
    """特許。日本の出願と、米国での段階を分けて書く。

    「取得」と「公開」を同じ形で並べると、米国特許を 2 件持っているように
    読めてしまう。実際に登録されているのは片方だけなので、そこは必ず書き分ける。
    """
    items = rm.get("industrial_property_rights") or []
    if not items:
        return ""
    rows = []
    for it in sorted(items, key=lambda x: (x.get("application_date") or ""), reverse=True):
        title_en = rm_text(it.get("industrial_property_right_title"), "en")
        title_ja = rm_text(it.get("industrial_property_right_title"), "ja")

        inv_en = [x.get("name", "") for x in ((it.get("inventors") or {}).get("en") or [])]
        inv_ja = [x.get("name", "") for x in ((it.get("inventors") or {}).get("ja") or [])]
        mark = lambda seq, me: ", ".join(
            ('<span class="me">%s</span>' % esc(x)) if x.replace(" ", "") in me else esc(x)
            for x in seq)
        # 本人の名前を強調するため中に <span> が入る。data-en / data-ja は
        # textContent で差し替える仕組みなので、印の付いた文字列は入れられない。
        # ここだけ only-en / only-ja の 2 本立てにして CSS で出し分ける。
        who_en = mark(inv_en, {"TakuyaKawasaki"})
        who_ja = mark(inv_ja, {"川﨑拓也", "川崎拓也"})

        holders = " / ".join(
            esc(rm_text(a.get("applicant"), "en")) for a in (it.get("applicants") or []))

        tail = []
        stage, number, url = us_patent(it.get("see_also"))
        if stage == "granted":
            tail.append('<span class="pos" data-en="US patent granted" '
                        'data-ja="米国登録">US patent granted</span>')
        elif stage == "published":
            tail.append('<span class="collab" data-en="US application published" '
                        'data-ja="米国公開">US application published</span>')
        if number:
            tail.append('<a href="%s" rel="noopener">%s</a>' % (esc(url), esc(number)))
        if it.get("application_number"):
            tail.append('<span class="cited">%s</span>' % esc(it["application_number"]))

        rows.append(
            '<li class="pub">\n'
            '  <div class="pub-year">%s</div>\n'
            '  <div>\n'
            '    <h3 class="pub-title" data-en="%s" data-ja="%s">%s</h3>\n'
            '    <p class="pub-meta"><span class="only-en">%s</span>'
            '<span class="only-ja">%s</span>'
            ' &mdash; <span>%s</span></p>\n'
            '    <div class="pub-tail">%s</div>\n'
            '  </div>\n'
            '</li>' % (
                esc((it.get("application_date") or "")[:4]),
                esc(title_en), esc(title_ja), esc(title_en),
                who_en, who_ja, holders, "".join(tail)))
    return ('<section id="patents" class="block">%s<ol class="ledger">%s</ol></section>'
            % (head("patents"), "".join(rows)))


def talks(rm):
    """学会発表。researchmap の presentations をそのまま並べる。

    国際会議と国内学会は分ける。日本の審査では、この 2 つは別の欄として
    読まれるので、混ぜると数えにくい。判定は researchmap の
    presentation_type ではなく、後述の是非がはっきりしている
    「発表が国際会議かどうか」のフラグ（event_en の有無ではなく）を使う。
    """
    items = rm.get("presentations") or []
    if not items:
        return ""

    def when(it):
        return talk_date(it)

    intl, dom = [], []
    for it in sorted(items, key=when, reverse=True):
        (intl if is_international(it) else dom).append(it)

    out = []
    for key, group in (("talks_intl", intl), ("talks_dom", dom)):
        if not group:
            continue
        rows = "".join(talk_row(it) for it in group)
        en, ja = PROFILE["en"]["labels"], PROFILE["ja"]["labels"]
        count = ('<span class="groupnum"><span data-en="%s" data-ja="%s">%s</span></span>'
                 % (esc("%d talks" % len(group)), esc("%d 件" % len(group)),
                    esc("%d talks" % len(group))))
        out.append('<div class="talk-group"><div class="grouphead">%s%s</div>'
                   '<ol class="ledger">%s</ol></div>'
                   % (bilingual("h3", en[key], ja[key], "subhead"), count, rows))
    return ('<section id="talks" class="block">%s%s</section>'
            % (head("talks"), "".join(out)))


def is_international(it):
    """researchmap の is_international_presentation をそのまま使う。

    会議名から推測してもよさそうに見えるが、日本で開かれる国際会議も、
    英語名を持つ国内の研究会もあるので当たらない。未設定のものは
    国内扱いにする。推測で「国際会議」に積み増すと件数の水増しになる。
    """
    return it.get("is_international_presentation") is True


def talk_date(it):
    """講演の日付。ここだけ他の欄と名前が違う。

    経歴や科研費は from_date / to_date だが、講演は
    publication_date（発表年月日）と from_event_date（開催年月日）を使う。
    発表日が入っていないものは開催初日で代用する。
    """
    return (it.get("publication_date") or it.get("from_event_date") or "")


def talk_row(it):
    title_en = rm_text(it.get("presentation_title"), "en")
    title_ja = rm_text(it.get("presentation_title"), "ja")
    ev_en = rm_text(it.get("event"), "en")
    ev_ja = rm_text(it.get("event"), "ja")
    place_en = rm_text(it.get("location"), "en")
    place_ja = rm_text(it.get("location"), "ja")

    # 国際会議は日本語表示でも英語のまま出す。
    # researchmap は日英どちらの題目も必須なので和題を入れてあるが、
    # それは登録のために用意した訳であって、実際に発表した演題ではない。
    # 訳を日本語ページに出すと、公式の演題のように読まれてしまう。
    # 国内学会も日本のCVの慣習どおり、英語ページでだけ訳を出す。
    if is_international(it):
        title_ja = title_en or title_ja
        ev_ja = ev_en or ev_ja
        place_ja = place_en or place_ja

    frm = talk_date(it)
    year = frm[:4]
    when = frm[:7].replace("-", ".")

    meta_en = " &mdash; ".join(x for x in (esc(ev_en), esc(place_en)) if x)
    meta_ja = " &mdash; ".join(x for x in (esc(ev_ja or ev_en), esc(place_ja or place_en)) if x)

    tail = []
    kind = (it.get("presentation_type") or "")
    if "invited" in kind:
        tail.append('<span class="pos" data-en="%s" data-ja="%s">%s</span>'
                    % (esc(PROFILE["en"]["labels"]["invited"]),
                       esc(PROFILE["ja"]["labels"]["invited"]),
                       esc(PROFILE["en"]["labels"]["invited"])))
    if "poster" in kind:
        tail.append('<span class="collab" data-en="poster" data-ja="ポスター">poster</span>')
    elif "oral" in kind:
        tail.append('<span class="collab" data-en="oral" data-ja="口頭">oral</span>')
    if when and when != year:
        tail.append('<span class="cited">%s</span>' % esc(when))

    return (
        '<li class="pub">\n'
        '  <div class="pub-year">%s</div>\n'
        '  <div>\n'
        '    <h3 class="pub-title" data-en="%s" data-ja="%s">%s</h3>\n'
        '    <p class="pub-meta"><span class="only-en">%s</span>'
        '<span class="only-ja">%s</span></p>\n'
        '    <div class="pub-tail">%s</div>\n'
        '  </div>\n'
        '</li>' % (esc(year),
                   esc(title_en or title_ja), esc(title_ja or title_en), esc(title_en or title_ja),
                   meta_en or meta_ja, meta_ja or meta_en, "".join(tail))
    )


def news():
    items = [n for n in PROFILE.get("news", []) if n.get("date")]
    if not items:
        return ""      # 中身が無い欄は出さない
    rows = "".join(
        '<li><time datetime="%s">%s</time>%s</li>'
        % (esc(n["date"]), esc(n["date"].replace("-", ".")),
           bilingual("p", n.get("en", ""), n.get("ja", n.get("en", ""))))
        for n in items
    )
    return ('<section id="news" class="block">%s<ul class="news">%s</ul></section>'
            % (head("news"), rows))


def contact():
    en, ja = PROFILE["en"]["contact"], PROFILE["ja"]["contact"]
    rows = []
    for (ek, ev), (jk, jv) in zip(en.items(), ja.items()):
        val = ('<a href="mailto:%s">%s</a>' % (esc(ev), esc(ev))) if "@" in str(ev) else None
        rows.append('<div>%s%s</div>' % (
            bilingual("dt", ek, jk),
            ('<dd>%s</dd>' % val) if val else bilingual("dd", ev, jv)))
    return ('<section id="contact" class="block">%s<dl class="contact">%s</dl></section>'
            % (head("contact"), "".join(rows)))


def year_ledger(pubs, sel):
    """年で区切った台帳。1 つのまとまりの中身を組むだけ。"""
    labels = sel.get("labels", {})
    parts, current = [], None
    for p in pubs:
        y = p.get("year") or "—"
        if y != current:
            if current is not None:
                parts.append("</ol>")
            n = sum(1 for q in pubs if (q.get("year") or "—") == y)
            parts.append('<div class="yearsep"><h2>%s</h2><span>%d</span></div><ol class="ledger">'
                         % (esc(y), n))
            current = y
        parts.append(citation_html(p, labels))
    if current is not None:
        parts.append("</ol>")
    return "".join(parts)


def group_note(key, pubs):
    """まとまりごとに件数と被引用を出す。合計だけを大きく出すと、
    その数字がどこから来たのかが見えなくなる。"""
    en, ja = PROFILE["en"]["labels"], PROFILE["ja"]["labels"]
    c = sum(p.get("cited_by") or 0 for p in pubs)
    return ('<div class="grouphead">%s<span class="groupnum">'
            '<span data-en="%s" data-ja="%s">%s</span>'
            '<span data-en="%s" data-ja="%s">%s</span></span></div>'
            % (bilingual("h2", en[key], ja[key]),
               esc("%d papers" % len(pubs)), esc("%d 件" % len(pubs)),
               esc("%d papers" % len(pubs)),
               esc("%s citations" % f"{c:,}"), esc("被引用 %s" % f"{c:,}"),
               esc("%s citations" % f"{c:,}")))


def all_publications(pubs, sel):
    """著者順が意味を持つ論文と、千人規模の共同研究論文を分けて並べる。

    混ぜると、本人が何をした論文なのかが 30 件の共同研究論文に埋もれる。
    分けても件数と被引用は各まとまりに出すので、隠したことにはならない。
    """
    own = [p for p in pubs if (p.get("n_authors") or 0) < COLLAB_MIN]
    collab = [p for p in pubs if (p.get("n_authors") or 0) >= COLLAB_MIN]

    out = []
    if own:
        out.append('<section id="papers" class="block">%s%s</section>'
                   % (group_note("papers", own), year_ledger(own, sel)))
    if collab:
        note = bilingual("p", PROFILE["en"]["labels"]["collab_note"],
                         PROFILE["ja"]["labels"]["collab_note"], "note")
        out.append('<section id="collaboration" class="block">%s%s%s</section>'
                   % (group_note("collaboration", collab), note, year_ledger(collab, sel)))
    return "".join(out)


def pub_map(sections):
    """業績ページの脇に出す目次。

    節（論文・特許・学会発表）と年の区切りが同じ太さの罫線で並ぶと、
    どこが大きな区切りなのか読み取れない。罫線の強弱で直すのに加えて、
    種類で直接飛べる道を用意する。中身が無い節は出さない。
    """
    en, ja = PROFILE["en"]["labels"], PROFILE["ja"]["labels"]
    items = []
    for anchor, key, n in sections:
        if not n:
            continue
        items.append(
            '<li><a href="#%s"><span data-en="%s" data-ja="%s">%s</span>'
            '<span class="n">%d</span></a></li>'
            % (esc(anchor), esc(en[key]), esc(ja[key]), esc(en[key]), n))
    if len(items) < 2:
        return ""
    return ('<nav class="pubmap" aria-label="%s">%s<ol>%s</ol></nav>'
            % (esc(en["onthispage"]),
               bilingual("h2", en["onthispage"], ja["onthispage"]),
               "".join(items)))


def pub_head(pubs):
    en, ja = PROFILE["en"], PROFILE["ja"]
    # 被引用の合計はここには出さない。99% が千人規模の共同研究論文から来るので、
    # 一つの数字にまとめると何を表しているのか分からなくなる。
    # 代わりに、論文と共同研究論文それぞれの見出しに件数と被引用を出している。
    first = sum(1 for p in pubs
                if (p.get("n_authors") or 0) < ABBREV_MIN and author_position(p) == 1)
    years = [p["year"] for p in pubs if p.get("year")]
    span = "%d–%d" % (min(years), max(years)) if years else ""
    summary = (
        '<div class="summary">'
        '<span data-en="%s" data-ja="%s">%s</span>'
        '<span data-en="%s" data-ja="%s">%s</span>'
        '<span>%s</span>'
        '</div>' % (
            esc("%d publications" % len(pubs)), esc("業績 %d 件" % len(pubs)),
            esc("%d publications" % len(pubs)),
            esc("first author on %d" % first), esc("うち筆頭著者 %d 件" % first),
            esc("first author on %d" % first),
            esc(span))
    )
    return ('<section class="block" style="padding-block:44px 0">%s%s%s</section>'
            % (head("publications"), summary,
               bilingual("p", en["pub_caveat"], ja["pub_caveat"], "caveat")))


def jsonld():
    en = PROFILE["en"]
    same = [l["url"] for l in PROFILE["links"] if (l.get("url") or "").strip()]
    return json.dumps({
        "@context": "https://schema.org",
        "@type": "Person",
        "name": en["name"],
        # 「﨑」(U+FA11) と「崎」(U+5D0E) は Unicode 上まったく別の文字で、
        # NFC/NFKC のどれで正規化しても互いに変換されない（﨑 は分解写像を持たない
        # 互換漢字の例外字）。つまり「川崎拓也」で検索しても文字列としては一致しない。
        # 一方 﨑 は JIS X 0208 に無いため、Shift_JIS / EUC-JP 前提の古いシステムでは
        # そもそも入力できない。結果として両方の表記が世の中に散らばる。
        # 表示は 﨑 を正としつつ、両方を機械可読な別名として持たせて取りこぼしを防ぐ。
        "alternateName": PROFILE["ja"].get("also_known_as") or [PROFILE["ja"]["name"]],
        "jobTitle": en["role"],
        "email": "mailto:" + PROFILE["mailto"],
        "url": PROFILE["site_url"] + "/",
        "affiliation": {"@type": "Organization", "name": en["affiliation"]},
        "identifier": "https://orcid.org/" + PROFILE["orcid"],
        "sameAs": same,
        # 見出しとキーワードの両方を入れる。片方だけにすると、画面に出ている語が
        # 機械可読データから抜け落ちる。順序を保ったまま重複だけ落とす。
        "knowsAbout": list(dict.fromkeys(
            [t["title"] for t in en["themes"]]
            + [k for t in en["themes"] for k in t.get("keys", [])])),
    }, ensure_ascii=False, indent=1)


def asset_stamp(rel):
    """CSS と JS のファイル名に中身のハッシュを付ける。付けないと、配色や
    レイアウトを直しても、一度見た人のブラウザには古いものが出続ける。"""
    try:
        with open(os.path.join(ROOT, rel), "rb") as f:
            return hashlib.md5(f.read()).hexdigest()[:8]
    except OSError:
        return ""


def render(tpl_name, out_name, marks):
    with open(os.path.join(TPL, tpl_name), encoding="utf-8") as f:
        s = f.read()
    for k, v in marks.items():
        token = "<!--#%s#-->" % k
        if token not in s:
            print("  ! 雛形に %s がありません" % token)
        s = s.replace(token, v)
    for rel in ("assets/site.css", "assets/site.js"):
        stamp = asset_stamp(rel)
        if stamp:
            s = s.replace('"%s"' % rel, '"%s?v=%s"' % (rel, stamp))

    left = re.findall(r"<!--#(\w+)#-->", s)
    if left:
        print("  ! 未置換のマーカー:", ", ".join(sorted(set(left))))
    with open(os.path.join(ROOT, out_name), "w", encoding="utf-8") as f:
        f.write(s)
    print("  → %s (%d bytes)" % (out_name, len(s.encode())))


# ---------------------------------------------------------------- 本体

def main():
    global PROFILE, UA, MAILTO, COLLAB_MIN, ABBREV_MIN, VENUE_OVERRIDES, SELF_SHORT
    with open(os.path.join(DATA, "profile.json"), encoding="utf-8") as f:
        PROFILE = json.load(f)
    with open(os.path.join(DATA, "selected.json"), encoding="utf-8") as f:
        sel = json.load(f)
    MAILTO = PROFILE["mailto"]
    COLLAB_MIN = PROFILE.get("collaboration_threshold", 100)
    ABBREV_MIN = PROFILE.get("abbreviate_threshold", 20)
    SELF_SHORT = "%s. %s" % ((PROFILE["author_match"]["given_prefix"] or "?").upper()[:1],
                             PROFILE["author_match"]["family"])
    VENUE_OVERRIDES = sel.get("venue_overrides", {})
    UA = "tkawasaki-phys.github.io sync.py (mailto:%s)" % MAILTO

    cached = {}
    if os.path.exists(CACHE):
        with open(CACHE, encoding="utf-8") as f:
            blob = json.load(f)
        cached = {p["doi"]: p for p in blob.get("publications", [])}
        fetched_at = blob.get("fetched_at", "")
    else:
        fetched_at = ""

    pubs = list(cached.values())

    if not OFFLINE:
        print("ORCID %s から名簿を取得" % PROFILE["orcid"])
        try:
            roster = orcid_dois(PROFILE["orcid"])
        except Exception as exc:                       # noqa: BLE001
            print("  ! ORCID が取れませんでした: %s" % exc)
            if not cached:
                sys.exit("キャッシュも無いので中止します。")
            print("  キャッシュ %d 件でページだけ作り直します。" % len(cached))
            roster = None

        if roster is not None:
            print("  DOI %d 件" % len(roster))
            merged, failed = [], 0
            for i, item in enumerate(roster, 1):
                doi = item["doi"]
                old = cached.get(doi)
                try:
                    rec = crossref(doi)
                except Exception as exc:               # noqa: BLE001
                    failed += 1
                    print("  ! Crossref 失敗 %s (%s)" % (doi, str(exc)[:60]))
                    if old:
                        merged.append(old)
                    continue
                rec["arxiv"] = item.get("arxiv") or (old or {}).get("arxiv", "")
                rec["cited_by"] = (old or {}).get("cited_by", 0)
                merged.append(rec)
                if i % 10 == 0:
                    print("  Crossref %d/%d" % (i, len(roster)))
                time.sleep(0.05)

            try:
                cites = openalex_citations([p["doi"] for p in merged])
                for p in merged:
                    if p["doi"] in cites:
                        p["cited_by"] = cites[p["doi"]]
                print("  OpenAlex 被引用数 %d/%d 件" % (len(cites), len(merged)))
            except Exception as exc:                   # noqa: BLE001
                print("  ! OpenAlex 失敗、前回の被引用数を使います: %s" % str(exc)[:80])

            if merged:
                pubs = merged
                fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if failed:
                print("  %d 件は取得できず、キャッシュの値を使いました。" % failed)

    # 正誤表（Correction / Erratum）のように、元論文と二重に並んでしまう項目を
    # 一覧から外す。ORCID 側からは消さない。記録としては正しいので、
    # 消すべきなのは「業績一覧の 1 行として数えること」だけ。
    exclude = {d.lower().strip() for d in sel.get("exclude", [])}
    if exclude:
        present = {p["doi"] for p in pubs}
        pubs = [p for p in pubs if p["doi"] not in exclude]
        n = len(present & exclude)
        if n:
            print("  selected.json の exclude で %d 件を非表示" % n)
        for d in sorted(exclude - present):
            print("  ! exclude の DOI が ORCID の業績に無い:", d)

    if not pubs:
        sys.exit("業績が 0 件です。書き出しません。")

    pubs.sort(key=lambda p: (-(p.get("year") or 0), (p.get("container") or ""), p.get("title") or ""))
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump({"fetched_at": fetched_at, "source": "ORCID %s → Crossref + OpenAlex" % PROFILE["orcid"],
                   "count": len(pubs), "publications": pubs}, f, ensure_ascii=False, indent=1)
    print("  → data/publications.json (%d 件)" % len(pubs))

    # ---- researchmap（経歴・学歴・科研費・特許）----
    # 論文と同じく、取れなかった日に欄が消えないようキャッシュを残す。
    rm = {}
    if os.path.exists(RM_CACHE):
        with open(RM_CACHE, encoding="utf-8") as f:
            rm = json.load(f).get("sections", {})
    if not OFFLINE and PROFILE.get("researchmap"):
        print("researchmap %s から経歴・科研費・特許を取得" % PROFILE["researchmap"])
        got = researchmap(PROFILE["researchmap"])
        if got:
            rm = dict(rm, **got)             # 取れた欄だけ差し替える
            with open(RM_CACHE, "w", encoding="utf-8") as f:
                json.dump({"fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                           "source": "https://api.researchmap.jp/%s" % PROFILE["researchmap"],
                           "sections": rm}, f, ensure_ascii=False, indent=1)
            # _profile は件数ではなくプロフィール本体なので、この行では数えない
            print("  " + " / ".join("%s %d" % (k, len(v))
                                    for k, v in sorted(rm.items()) if not k.startswith("_")))

    en = PROFILE["en"]
    # 検索結果に出る一文。だいたい 160 字までしか表示されないので、
    # 語の途中で切れないように最後の空白で落とす。
    first_sentence = re.split(r"(?<=[.。])\s", en["statement"].strip())[0]
    # 所属をフルで書くと 100 字以上を食って、肝心の一文が途中で切れる。
    # 検索結果に出るのは 160 字前後なので、ここだけ大学名に縮める。
    short_aff = en["affiliation"].split(",")[-1].strip()
    desc = "%s — %s, %s. %s" % (en["name"], en["role"], short_aff, first_sentence)
    if len(desc) > 185:
        desc = desc[:185].rsplit(" ", 1)[0].rstrip(" ,;—-") + "…"
    common = {"URL": esc(PROFILE["site_url"]), "BAR": "", "FOOTER": footer(fetched_at)}

    render("index.html", "index.html", dict(common, **{
        "TITLE": esc("%s · %s" % (en["name"], PROFILE["ja"]["name"])),
        "DESC": esc(desc),
        "JSONLD": jsonld(),
        "BAR": bar("research"),
        "HERO": hero(),
        "THEMES": themes(),
        "FEATURED": featured(pubs, sel),
        "CAREER": career(rm),
        "GRANTS": grants(rm),
        "NEWS": news(),
        "CONTACT": contact(),
    }))
    render("publications.html", "publications.html", dict(common, **{
        "TITLE": esc("Publications · %s" % en["name"]),
        "DESC": esc("Complete publication list of %s (%d items), generated from ORCID %s."
                    % (en["name"], len(pubs), PROFILE["orcid"])),
        "BAR": bar("publications"),
        "PUBHEAD": pub_head(pubs),
        "PUBMAP": pub_map([
            ("papers", "papers", sum(1 for p in pubs if (p.get("n_authors") or 0) < COLLAB_MIN)),
            ("collaboration", "collaboration", sum(1 for p in pubs if (p.get("n_authors") or 0) >= COLLAB_MIN)),
            ("patents", "patents", len(rm.get("industrial_property_rights") or [])),
            ("talks", "talks", len(rm.get("presentations") or [])),
        ]),
        "ALLPUBS": all_publications(pubs, sel),
        "PATENTS": patents(rm),
        "TALKS": talks(rm),
    }))
    print("完了。最終同期 %s" % (fetched_at or "（未取得）"))


if __name__ == "__main__":
    main()
