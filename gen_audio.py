#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
タイ語マスター — 不足音声の生成 / audio-manifest.json 更新

  python3 gen_audio.py --dry-run          不足分を一覧するだけ(APIを呼ばない)
  python3 gen_audio.py --voice th-TH-Chirp3-HD-XXXX   実際に生成

前提:
  gcloud auth application-default login
  gcloud services enable texttospeech.googleapis.com
"""
import argparse, base64, hashlib, json, os, subprocess, sys, time
import urllib.request, urllib.error

ROOT      = os.path.dirname(os.path.abspath(__file__))
COURSES   = os.path.join(ROOT, 'courses.json')
MANIFEST  = os.path.join(ROOT, 'audio-manifest.json')
AUDIO_DIR = os.path.join(ROOT, 'audio')
ENDPOINT  = 'https://texttospeech.googleapis.com/v1/text:synthesize'

# 既存3,422件と同一の命名規則。絶対に変えないこと。
def fname(text: str) -> str:
    return hashlib.md5(text.encode('utf-8')).hexdigest()[:12] + '.mp3'

# 「マニフェストのキー」と「実際に読ませる文字列」を分けるための対応表。
# ファイル名は必ずキー側の md5 で決まる(アプリは表示テキストで引くため)。
# 声調記号の項目は U+25CC(◌)が入っており、そのまま読ませると誤読するので除去する。
SAY_AS = {
    '◌่ ไม้เอก':    'ไม้เอก',
    '◌้ ไม้โท':     'ไม้โท',
    '◌๊ ไม้ตรี':    'ไม้ตรี',
    '◌๋ ไม้จัตวา':  'ไม้จัตวา',
    '◌็ ไม้ไต่คู้':  'ไม้ไต่คู้',
    '◌์ การันต์':   'การันต์',
}

def say_as(text: str) -> str:
    return SAY_AS.get(text, text)

def collect_texts():
    """courses.json 内の全タイ語文字列(文 + 語彙)を出現順に重複なく集める。"""
    d = json.load(open(COURSES, encoding='utf-8'))
    seen, out = set(), []
    for course in d.values():
        for unit in course['units']:
            for s in unit['sentences']:
                for t in [s['th']] + [w['th'] for w in s.get('words', [])]:
                    if t not in seen:
                        seen.add(t); out.append(t)
    return out

def token():
    return subprocess.run(['gcloud', 'auth', 'application-default', 'print-access-token'],
                          capture_output=True, text=True, check=True).stdout.strip()

# ADC の quota_project_id はクライアントライブラリが読む値。生のRESTで叩く場合は
# x-goog-user-project を自分で送らないと 403 SERVICE_DISABLED になる。
ADC = os.path.expanduser('~/.config/gcloud/application_default_credentials.json')

def quota_project():
    if os.path.exists(ADC):
        return json.load(open(ADC)).get('quota_project_id')
    return None

def synth(text, voice, rate, tok, tries=4):
    body = json.dumps({
        'input':       {'text': text},          # Chirp 3: HD は SSML 非対応 — plain text のみ
        'voice':       {'languageCode': 'th-TH', 'name': voice},
        'audioConfig': {'audioEncoding': 'MP3', 'speakingRate': rate},
    }).encode('utf-8')
    head = {'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json; charset=utf-8'}
    qp = quota_project()
    if qp:
        head['x-goog-user-project'] = qp
    # 数百件を連続で回すと DNS 断や 503 を踏む。一過性のものだけ指数バックオフで粘る。
    for n in range(tries):
        req = urllib.request.Request(ENDPOINT, data=body, headers=head)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return base64.b64decode(json.loads(r.read())['audioContent'])
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or n == tries - 1:
                raise
        except urllib.error.URLError:
            if n == tries - 1:
                raise
        time.sleep(2 ** n)

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--voice', default=None, help='例: th-TH-Chirp3-HD-Achernar')
    p.add_argument('--rate',  type=float, default=0.9, help='既存音声と同じ 0.9 が既定')
    p.add_argument('--dry-run', action='store_true')
    a = p.parse_args()

    manifest = json.load(open(MANIFEST, encoding='utf-8'))
    texts    = collect_texts()
    missing  = [t for t in texts
                if t not in manifest or not os.path.exists(os.path.join(AUDIO_DIR, manifest[t]))]

    print(f'courses.json のタイ語文字列: {len(texts)}')
    print(f'manifest 登録済み          : {len(manifest)}')
    print(f'音声が不足                 : {len(missing)}')
    if missing:
        chars = sum(len(t) for t in missing)
        print(f'合成文字数                 : {chars} 文字\n')
        for t in missing:
            print(f'   {fname(t)}  {t}')
    # courses.json から参照されなくなった孤立キー
    orphan = [k for k in manifest if k not in set(texts)]
    if orphan:
        print(f'\n孤立キー(参照なし): {len(orphan)}')
        for k in orphan:
            print(f'   {manifest[k]}  {k}')

    if a.dry_run or not missing:
        return
    if not a.voice:
        sys.exit('\n--voice が未指定です。既存音声と同じ話者を必ず指定してください。')

    os.makedirs(AUDIO_DIR, exist_ok=True)
    tok, ok, ng = token(), 0, []
    for i, t in enumerate(missing, 1):
        fn, spoken = fname(t), say_as(t)
        try:
            mp3 = synth(spoken, a.voice, a.rate, tok)
            open(os.path.join(AUDIO_DIR, fn), 'wb').write(mp3)
            manifest[t] = fn
            ok += 1
            note = '' if spoken == t else f'  (読み: {spoken})'
            print(f'  [{i}/{len(missing)}] {fn}  {t}{note}  ({len(mp3)}B)')
        except urllib.error.HTTPError as e:
            ng.append((t, f'{e.code} {e.read().decode("utf-8", "replace")[:200]}'))
        except Exception as e:
            ng.append((t, repr(e)))
        time.sleep(0.1)          # 控えめなレート制御

    # 既存ファイルと同じコンパクト1行形式で書き戻す
    open(MANIFEST, 'w', encoding='utf-8').write(
        json.dumps(manifest, ensure_ascii=False, separators=(',', ':')))
    print(f'\n生成 {ok} 件 / 失敗 {len(ng)} 件')
    for t, err in ng:
        print(f'   FAILED  {t}  -> {err}')

if __name__ == '__main__':
    main()
