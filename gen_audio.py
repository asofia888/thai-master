#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
タイ語マスター — 音声の生成 / audio-manifest.json 更新 (Gemini 3.8 Flash TTS)

  python3 gen_audio.py --dry-run     不足分を一覧するだけ(APIを呼ばない)
  python3 gen_audio.py               不足分を生成(途中で止めても再実行で続きから)
  python3 gen_audio.py --prune       manifest から参照されない audio/*.mp3 を削除

前提:
  gcloud auth application-default login
  quota project で Vertex AI API (aiplatform.googleapis.com) を有効化
  MP3 エンコーダ: python3 -m pip install --user lameenc
    (PEP 668 で止められたら --target <dir> で入れて PYTHONPATH=<dir> を付けて実行)

Cloud TTS API は gemini-3.8-flash-tts 非対応、Gemini API(generativelanguage)は
ADC のスコープ不足で 403 になるため、Vertex AI 経由で呼ぶ。
"""
import argparse, array, base64, hashlib, io, json, math, os, random, subprocess, sys, threading, time, wave
import urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT      = os.path.dirname(os.path.abspath(__file__))
COURSES   = os.path.join(ROOT, 'courses.json')
MANIFEST  = os.path.join(ROOT, 'audio-manifest.json')
AUDIO_DIR = os.path.join(ROOT, 'audio')

MODEL = 'gemini-3.8-flash-tts'
VOICE = 'Algenib'          # 旧 th-TH-Chirp3-HD-Algenib と同名の声
# 'slightly slow' と書くと文が Chirp の1.3〜2.6倍の長さになった。速さは普段どおりと明示する。
STYLE = ('Clear, neutral standard Central Thai, like a native teacher reading a textbook '
         'example aloud. Natural everyday speaking pace, not slowed down. Every tone precise.')
SR = 24000
TARGET_RMS = 4200          # 旧 Chirp 音声(無音を除く)の中央値。全クリップをこの音量に揃える

# MODEL / VOICE / STYLE を変えたら TAG も変えること。ファイル名が変わって全件が作り直しになり、
# /audio/ の immutable キャッシュや SW の cache-first に古い音声が残る問題も避けられる。
TAG = 'g38'

def fname(text: str) -> str:
    return hashlib.md5(text.encode('utf-8')).hexdigest()[:12] + '-' + TAG + '.mp3'

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

# 単語を単独で読ませると、綴りと違う声調で読むことがある(สี を สี่ と読む等)。
# 書き起こし照合で外れたものに限り、発音表記を添えて声調を指定する。キーは表示テキスト。
PRON_HINT = {
    "นวด": "nûat, falling tone",
    "โป้ง": "pôong, falling tone",
    "บาน": "baan, mid tone",
    "แน่น": "nɛ̂n, falling tone",
    "สี": "sǐi, rising tone",
    "แชมป์": "chɛɛm, mid tone",
    "เอีย": "iia, mid tone",
    "เก่า": 'kào, low tone, meaning "old" (not เก้า "nine", which is falling tone)',
    "โพสต์": 'phóot, high tone (the English loanword "post")',
}

def style_for(text: str) -> str:
    hint = PRON_HINT.get(text)
    return STYLE + (f' This is a single word pronounced "{hint}"; use exactly this tone.' if hint else '')

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

# ADC の quota_project_id はクライアントライブラリが読む値。生のRESTで叩く場合は
# x-goog-user-project を自分で送らないと 403 SERVICE_DISABLED になる。
ADC = os.path.expanduser('~/.config/gcloud/application_default_credentials.json')

def quota_project():
    if os.path.exists(ADC):
        return json.load(open(ADC)).get('quota_project_id')
    return None

class Auth:
    """ADC のアクセストークン。1時間で失効するので50分ごとに取り直す(並列実行でも1回だけ)。"""
    def __init__(self):
        self.lock, self.tok, self.at = threading.Lock(), None, 0.0

    def headers(self):
        with self.lock:
            if time.time() - self.at > 3000:
                self.tok = subprocess.run(['gcloud', 'auth', 'application-default', 'print-access-token'],
                                          capture_output=True, text=True, check=True).stdout.strip()
                self.at = time.time()
            tok = self.tok
        h = {'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json; charset=utf-8'}
        qp = quota_project()
        if qp:
            h['x-goog-user-project'] = qp
        return h

class Transient(Exception):
    pass

def request_audio(text, auth, style=STYLE):
    url = (f'https://aiplatform.googleapis.com/v1/projects/{quota_project()}/locations/global'
           f'/publishers/google/models/{MODEL}:generateContent')
    body = json.dumps({
        'contents': [{'role': 'user', 'parts': [{'text': text, 'speech_metadata': {'style': style}}]}],
        'generationConfig': {
            'responseModalities': ['AUDIO'],
            'responseFormat': {'audio': {'mimeType': 'AUDIO_WAV', 'sampleRate': SR}},
            'speechConfig': {'voiceConfig': {'voice': VOICE}},
        },
    }).encode('utf-8')
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=auth.headers()), timeout=120) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        msg = f'{e.code} {e.read().decode("utf-8", "replace")[:200]}'
        if e.code in (429, 500, 502, 503, 504):
            raise Transient(msg)
        raise RuntimeError(msg)
    except (urllib.error.URLError, TimeoutError) as e:
        raise Transient(repr(e))
    for p in d.get('candidates', [{}])[0].get('content', {}).get('parts', []):
        if 'inlineData' in p:
            with wave.open(io.BytesIO(base64.b64decode(p['inlineData']['data']))) as w:
                assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, SR)
                return array.array('h', w.readframes(w.getnframes()))
    # 音声の代わりにテキストが返ることがある(既知の不具合)。作り直せば通る。
    raise Transient('音声が返らなかった')

def speech_span(pcm):
    """(発話区間の秒数, 区間内の最長無音の秒数)。10ms 窓で最大窓の -30dB 未満を無音とみなす。"""
    win = SR // 100
    e = [sum(x * x for x in pcm[i:i + win]) for i in range(0, len(pcm) - win + 1, win)]
    if not e or max(e) == 0:
        return 0.0, 0.0
    th = max(e) * 1e-3
    act = [i for i, v in enumerate(e) if v > th]
    gap = longest = 0
    for v in e[act[0]:act[-1] + 1]:
        gap = gap + 1 if v <= th else 0
        longest = max(longest, gap)
    return (act[-1] - act[0] + 1) / 100, longest / 100

def normalize(pcm):
    """無音を除いた RMS を TARGET_RMS に揃える(クリップはさせない)。"""
    win = SR // 50
    e = [sum(x * x for x in pcm[i:i + win]) / win for i in range(0, len(pcm) - win + 1, win)]
    top = max(e) if e else 0
    keep = [v for v in e if v > top * 1e-4]
    if not keep:
        return pcm
    g = TARGET_RMS / math.sqrt(sum(keep) / len(keep))
    g = min(g, 32000 / (max(abs(x) for x in pcm) or 1))
    return array.array('h', (int(x * g) for x in pcm))

def encode_mp3(pcm):
    import lameenc
    enc = lameenc.Encoder()
    enc.set_bit_rate(32); enc.set_in_sample_rate(SR); enc.set_channels(1); enc.set_quality(2)
    return enc.encode(pcm.tobytes()) + enc.flush()

def synth(text, auth, tries=6):
    """生成して MP3 を返す。数千件を並列で回すと 429/503 や音声なし応答を踏むので、一過性のものは粘る。"""
    for n in range(tries):
        try:
            pcm = request_audio(say_as(text), auth, style_for(text))
            span, gap = speech_span(pcm)
            if span < 0.1:
                raise Transient(f'ほぼ無音 ({span:.2f}s)')
            return encode_mp3(normalize(pcm)), span, gap
        except Transient:
            if n == tries - 1:
                raise
            time.sleep(2 ** n + random.random())

def save_manifest(manifest):
    # 既存ファイルと同じコンパクト1行形式で書き戻す
    tmp = MANIFEST + '.tmp'
    open(tmp, 'w', encoding='utf-8').write(json.dumps(manifest, ensure_ascii=False, separators=(',', ':')))
    os.replace(tmp, MANIFEST)

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--prune', action='store_true', help='manifest から参照されない audio/*.mp3 を削除')
    p.add_argument('--jobs', type=int, default=6, help='同時リクエスト数')
    p.add_argument('--redo', nargs='*', default=[], help='指定したタイ語文字列を作り直す')
    p.add_argument('--limit', type=int, help='先頭から N 件だけ生成する(試運転用)')
    a = p.parse_args()

    manifest = json.load(open(MANIFEST, encoding='utf-8'))
    texts    = collect_texts()
    redo     = set(a.redo)
    missing  = [t for t in texts
                if t in redo or manifest.get(t) != fname(t)
                or not os.path.exists(os.path.join(AUDIO_DIR, fname(t)))]

    print(f'courses.json のタイ語文字列: {len(texts)}')
    print(f'manifest 登録済み          : {len(manifest)}')
    print(f'生成が必要                 : {len(missing)}')
    if missing:
        print(f'合成文字数                 : {sum(len(t) for t in missing)} 文字\n')
        for t in missing[:50]:
            print(f'   {fname(t)}  {t}')
        if len(missing) > 50:
            print(f'   … ほか {len(missing) - 50} 件')
    # courses.json から参照されなくなった孤立キー
    orphan = [k for k in manifest if k not in set(texts)]
    if orphan:
        print(f'\n孤立キー(参照なし): {len(orphan)}')
        for k in orphan:
            print(f'   {manifest[k]}  {k}')

    if a.prune:
        used = set(manifest.values())
        stale = sorted(f for f in os.listdir(AUDIO_DIR) if f.endswith('.mp3') and f not in used)
        for f in stale:
            os.remove(os.path.join(AUDIO_DIR, f))
        print(f'\n参照されない音声を削除: {len(stale)} 件')
        return
    if a.dry_run or not missing:
        return

    if a.limit:
        missing = missing[:a.limit]
    os.makedirs(AUDIO_DIR, exist_ok=True)
    auth = Auth()
    auth.headers()                       # 最初のトークン取得(WSL では数分かかる)をここで済ませる
    ok, ng, t0 = 0, [], time.time()

    def job(t):
        mp3, span, gap = synth(t, auth)
        open(os.path.join(AUDIO_DIR, fname(t)), 'wb').write(mp3)
        return t, len(mp3), span, gap

    with ThreadPoolExecutor(a.jobs) as ex:
        futs = {ex.submit(job, t): t for t in missing}
        for i, f in enumerate(as_completed(futs), 1):
            t = futs[f]
            try:
                _, size, span, gap = f.result()
                manifest[t] = fname(t)
                ok += 1
                note = '' if say_as(t) == t else f'  (読み: {say_as(t)})'
                print(f'  [{i}/{len(missing)}] {fname(t)}  {t}{note}  ({size}B, 発話 {span:.2f}s)', flush=True)
            except Exception as e:
                ng.append((t, repr(e)[:200]))
                print(f'  [{i}/{len(missing)}] FAILED  {t}  -> {ng[-1][1]}', flush=True)
            if i % 100 == 0:
                save_manifest(manifest)
                print(f'  -- {i} 件処理 / {time.time() - t0:.0f}s', flush=True)

    save_manifest(manifest)
    print(f'\n生成 {ok} 件 / 失敗 {len(ng)} 件')
    for t, err in ng:
        print(f'   FAILED  {t}  -> {err}')

if __name__ == '__main__':
    main()
