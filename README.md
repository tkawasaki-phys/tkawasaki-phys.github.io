# takuya-kawasaki.com

川﨑拓也（東京大学 大学院理学系研究科 物理学専攻）の個人サイト。

ビルドツールは使いません。Python 3 の標準ライブラリだけで動きます。npm も Jekyll も不要です。

公開先は <https://takuya-kawasaki.com>（GitHub Pages ＋ 独自ドメイン）。

## 中身

| | |
| --- | --- |
| `index.html`, `publications.html` | **生成物。直接編集しないでください**（次の同期で上書きされます） |
| `data/profile.json` | 氏名・所属・研究テーマ・連絡先・リンク。**本文を直すのはここ** |
| `data/selected.json` | トップの「主要業績」に出す DOI と、一覧から外す DOI |
| `data/publications.json` | ORCID から取った業績のキャッシュ。自動生成 |
| `data/researchmap.json` | researchmap から取った経歴などのキャッシュ。自動生成 |
| `templates/` | ページの骨組み。`<head>` やフォントを変えるときだけ触る |
| `tools/sync.py` | 外部から集めてページを組む。このリポジトリの本体 |
| `assets/site.css` | 配色と組版 |
| `CNAME` | 独自ドメインの指定。消すと `*.github.io` に戻ります |

## どこから何を取っているか

| 出どころ | 取るもの |
| --- | --- |
| **ORCID** | 業績の名簿（DOI のみ） |
| Crossref | 各 DOI の題名・誌名・著者・巻号頁 |
| OpenAlex | 各 DOI の被引用数（**著者検索はしない**。下の「業績の取り方」を参照） |
| **researchmap** | 職歴・学歴・担当科目・科研費・特許・学会発表 |

論文は ORCID、それ以外は researchmap、と役割を分けています。混ぜません。

## 更新のしかた

```bash
python3 tools/sync.py
```

毎週月曜 06:00 JST に GitHub Actions が同じことをして、差分があればコミットします
（`.github/workflows/sync.yml`）。手元で走らせる必要があるのは、急ぎで反映したいときだけです。

ネットに出ずページだけ組み直すなら `python3 tools/sync.py --offline`。
キャッシュを使うので、文面や配色をいじるときはこちらが速いです。

ローカルで見るには:

```bash
python3 -m http.server -d . 8000
```

## 本文を直す

`data/profile.json` の `en` と `ja` を直して `--offline` で組み直すだけです。

- **紹介文** — `statement`
- **研究テーマ** — `themes`。`title` / `body` / `keys`
- **ニュース** — `news` に `{"date": "2026-04", "en": "...", "ja": "..."}` を追記。
  空のままならニュース欄自体が出ません
- **リンク** — `links` の `url` を埋めると出ます。空のものは出ません
- **主要業績** — `data/selected.json` の `featured` に DOI を並べる。並べた順に出ます
- **一覧から外す** — `data/selected.json` の `exclude` に DOI。正誤表など、元論文と
  二重に並ぶものを外すためのもので、ORCID 側からは消しません

経歴・科研費・特許・学会発表・担当科目は **researchmap を直してください**。
ここを直しても変わりません。

## 見た目

**差し色** — `templates/*.html` の `<html ... data-accent="aomidori">` を `data-accent="suou"`
に変えると蘇芳になります。両方の配色が `assets/site.css` に入っています。

**紙の色** — `assets/site.css` の `--paper`（既定 `#FAF9F7`）。同じ行に、灰色寄りと白寄りの
候補値をコメントで書いてあります。

**サイトマーク** — `data/profile.json` の `seal_svg`（頭文字 T の角印・白文）。最初の「川」印と
同じ作りで、地を差し色で塗って字を抜いただけです。凸凹も枠の飾りも付けていません。`viewBox` は
`0 0 24 24`、地は `currentColor` なので差し色（青緑↔蘇芳）にもダークモードにも追従し、抜いた
字には紙の色が出ます。抜きは `mask` です。

字形は支給の `TK.svg`（筑紫B丸ゴシック Regular）から T をアウトライン化したものです。
**フォント名を参照していないので、閲覧者の環境に依存しません。** 字を変える・比率を変えるときは
アウトラインを取り直す必要があります。

`seal_svg` を空にすると `seal`（文字の "T"）に戻り、そのときは CSS で地を塗ります（`.seal-txt`）。
**同じ字形が `assets/favicon.svg` にも入っています。変えるときは両方です。**

**顔写真** — `assets/portrait.jpg` を置けば自動で使われます。無ければ頭文字の枠に戻ります。
縦横比は 4:5。

## 業績の取り方について

**ORCID に本人が登録した DOI だけを名簿とし、DOI を鍵に Crossref と OpenAlex を引きます。**
著者名でも OpenAlex の著者 ID でも検索しません。

OpenAlex の著者エンドポイントは同姓イニシャルの別人を統合します。実測では、この著者ページが
返す 80 件のうち **51 件が別人の業績**でした（Double Chooz・JSNS²・Belle の別の T. Kawasaki と、
計算機アーキテクチャの別人）。被引用数も 3,783 が 8,699 に膨らみます。DOI を鍵にすればこの
事故は起こりません。**この方針は変えないでください。**

業績は HTML に焼き込んでいます。実行時に JavaScript で取りに行くと、検索エンジンに拾われず、
API が落ちた日に業績欄が空白になるためです。

取得に失敗しても既存のキャッシュは壊しません。1 件でも取れなければ前回の値を使い、
全部失敗したら書き出しそのものを中止します。
