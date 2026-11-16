"""でちゃう！取材・来店スケジュールを取得し、VRChatギミック用のデータを作る。

出力（docs/ フォルダ。GitHub Pagesで公開される）
  schedule.json … 予定データ（全国＋10エリアの所属情報つき）
  thumbs.jpg    … サムネを1枚にまとめた画像（タイル状）

失敗したときは何も上書きせずに終了する（前回のデータがそのまま残る）。
"""
import datetime as dt
import hashlib
import io
import json
import math
import os
import sys
import time

import requests
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
CACHE = os.path.join(ROOT, "cache")

API = "https://www.dechau.com/schedule/search_api.php"
AREAS = [
    "北海道・東北", "北関東", "関東", "北陸・甲信越", "東海",
    "静岡", "関西", "中国・四国", "九州・山口", "熊本・鹿児島・宮崎・沖縄",
]
WEEKDAYS = "月火水木金土日"
JST = dt.timezone(dt.timedelta(hours=9))
ATLAS_MAX = 2048          # VRChatの画像読み込みの上限（2048×2048）
WAIT = 0.4                # サイトへの負荷を抑えるための間隔（秒）

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; dechau-vrc-schedule/1.0)",
    "Referer": "https://www.dechau.com/schedule/schedule_all.php",
})


def clean(text):
    """絵文字など、ギミックのフォントで表示できない文字を取り除く。"""
    text = "".join(c for c in (text or "") if ord(c) <= 0xFFFF and c not in "\ufe0f\u200d")
    return " ".join(text.split())


def fetch_all(params):
    """search_api.php を全ページ取得する。"""
    out, page, page_cnt = [], 1, 1
    while page <= page_cnt:
        r = session.get(API, params={**params, "page": page}, timeout=30)
        r.raise_for_status()
        j = r.json()
        page_cnt = int(j.get("pageCnt") or 1)
        out.extend(j.get("list") or [])
        page += 1
        time.sleep(WAIT)
        if page > 200:
            raise RuntimeError("ページ数が多すぎます")
    return out


def thumb_tile(url, size):
    """サムネを取得（キャッシュ優先）し、正方形 size×size にする。"""
    key = hashlib.sha1(url.encode()).hexdigest()[:20]
    path = os.path.join(CACHE, key + ".jpg")
    if os.path.exists(path):
        im = Image.open(path).convert("RGB")
    else:
        r = session.get(url, timeout=30)
        r.raise_for_status()
        im = Image.open(io.BytesIO(r.content)).convert("RGB")
        s = min(im.size)
        l, t = (im.width - s) // 2, (im.height - s) // 2
        im = im.crop((l, t, l + s, t + s)).resize((128, 128), Image.LANCZOS)
        im.save(path, quality=92, optimize=True)
        time.sleep(WAIT)
    if size != 128:
        im = im.resize((size, size), Image.LANCZOS)
    return im, key


def main():
    os.makedirs(DOCS, exist_ok=True)
    os.makedirs(CACHE, exist_ok=True)

    items = fetch_all({})
    if not items:
        sys.exit("予定が0件でした。サイトの状態を確認してください（前回のデータを維持します）。")

    member = {}
    for i, area in enumerate(AREAS):
        for x in fetch_all({"area": area}):
            member[x["id"]] = member.get(x["id"], 0) | (1 << i)

    # サムネの一覧（初登場順）
    thumbs, index = [], {}
    for x in items:
        u = x.get("thumbnail") or ""
        if u not in index:
            index[u] = len(thumbs)
            thumbs.append(u)

    # タイルの大きさを決める（2048×2048 に収まるように）
    for tile in (128, 96, 80, 64):
        cols = ATLAS_MAX // tile
        if math.ceil(len(thumbs) / cols) * tile <= ATLAS_MAX:
            break
    else:
        sys.exit("サムネが多すぎてまとめ画像に収まりません")
    rows = max(1, math.ceil(len(thumbs) / cols))
    atlas = Image.new("RGB", (cols * tile, rows * tile), (238, 238, 238))
    used = set()
    for i, u in enumerate(thumbs):
        if not u:
            continue
        try:
            im, key = thumb_tile(u, tile)
            used.add(key + ".jpg")
            atlas.paste(im, ((i % cols) * tile, (i // cols) * tile))
        except Exception as e:  # 1枚失敗しても全体は止めない
            print("サムネ取得失敗:", u, e)

    out_items = []
    for x in items:
        d = dt.date.fromisoformat(x["eventdate"][:10])
        out_items.append([
            f"{d.month}/{d.day}({WEEKDAYS[d.weekday()]})",
            clean(x.get("pref")),
            clean(x.get("cms_shop_name") or (x.get("cms_shop_address1") or "").split(",")[0]),
            clean(x.get("hmnm_title")),
            index[x.get("thumbnail") or ""],
            member.get(x["id"], 0),
        ])

    data = {
        "areas": AREAS, "tile": tile, "cols": cols,
        "aw": atlas.width, "ah": atlas.height, "items": out_items,
    }

    # 中身が変わっていなければ書き換えない（更新日時だけの変更でコミットしないため）
    digest = hashlib.sha1(json.dumps([data, thumbs], ensure_ascii=False).encode()).hexdigest()
    json_path = os.path.join(DOCS, "schedule.json")
    if os.path.exists(json_path):
        try:
            if json.load(open(json_path, encoding="utf-8")).get("hash") == digest:
                print("変更なし")
                return
        except Exception:
            pass

    data = {"updated": dt.datetime.now(JST).strftime("%Y-%m-%d %H:%M"), "hash": digest, **data}
    atlas.save(os.path.join(DOCS, "thumbs.jpg"), quality=88, optimize=True)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

    # 使われなくなったサムネのキャッシュを削除
    for name in os.listdir(CACHE):
        if name.endswith(".jpg") and name not in used:
            os.remove(os.path.join(CACHE, name))

    print(f"{len(out_items)}件 / サムネ{len(thumbs)}種 / タイル{tile}px / 画像{atlas.width}x{atlas.height}")


if __name__ == "__main__":
    main()
