# tkawasaki-phys.github.io

川﨑拓也（東京大学 大学院理学系研究科 物理学専攻）の個人サイト。
ビルドツールは使いません。Python 3 の標準ライブラリだけで動きます。

## 中身

| | |
| --- | --- |
| `index.html`, `publications.html` | **生成物。直接編集しないでください**（次回の同期で上書きされます） |
| `data/profile.json` | 氏名・所属・研究テーマ・連絡先・ニュース。**本文を直すのはここ** |
| `data/selected.json` | トップの「主要業績」に出す論文の DOI |
| `data/publications.json` | ORCID から取った業績のキャッシュ。自動生成 |
| `templates/` | ページの骨組み。`<head>` や読み込むフォントを変えるときだけ触る |
| `tools/sync.py` | ORCID から業績を集めてページを生成する |
| `assets/site.css` | 配色と組版。差し色の切り替えもここ |

## 更新のしかた

```bash
python3 tools/sync.py
```

ORCID の業績が増えていれば自動で反映されます。ネットに繋がずページだけ組み直すなら
`python3 tools/sync.py --offline`。

ローカルで見るには：

```bash
python3 -m http.server -d . 8000
```

ブラウザで `http://localhost:8000` を開きます。

## 本文を直す

`data/profile.json` の `en` と `ja` を直して `python3 tools/sync.py --offline` を走らせるだけです。

- **研究テーマ** — `themes`。`title` / `body` / `keys`
- **ニュース** — `news` に `{"date": "2026-04", "en": "...", "ja": "..."}` を追記。
  空のままならニュース欄自体が出ません
- **リンク** — `links` の `url` を埋めると出てきます。空のものは出ません
- **主要業績** — `data/selected.json` の `featured` に DOI を並べる。並べた順に出ます

## 差し色を蘇芳に戻す

`templates/*.html` の `<html ... data-accent="aomidori">` を `data-accent="suou"` に書き換えて、
`python3 tools/sync.py --offline` を走らせてください。両方の配色が `assets/site.css` に入っています。

紙の色は `assets/site.css` の `--paper`（既定 `#FAF9F7`）。同じ行に、灰色寄りと白寄りの候補値を
コメントで書いてあります。

## 顔写真

`assets/portrait.jpg` を置いて、`templates/` ではなく `tools/sync.py` の `hero()` にある
プレースホルダを `<img src="assets/portrait.jpg" alt="">` に差し替えてください
（該当箇所にコメントがあります）。縦横比は 4:5 です。

## 業績の取り方について

**ORCID に本人が登録した DOI だけを名簿とし、DOI を鍵に Crossref と OpenAlex を引きます。**
著者名でも OpenAlex の著者 ID でも検索しません。

OpenAlex の著者エンドポイントは同姓イニシャルの別人を統合します。実測では、このアカウントの
著者ページが返す 80 件のうち **51 件が別人の業績**でした（Double Chooz・JSNS²・Belle の別の
T. Kawasaki と、計算機アーキテクチャの別人）。被引用数も 3,783 が 8,699 に膨らみます。
DOI を鍵にすればこの事故は起こりません。この方針は変えないでください。

業績は HTML に焼き込んでいます。実行時に JavaScript で取りに行くと、検索エンジンに拾われず、
API が落ちた日に業績欄が空白になるためです。

## 公開するとき

まだ公開していません。公開する手順は別途。
