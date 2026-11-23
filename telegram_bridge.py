#!/usr/bin/env python3
"""Per-customer Telegram bot bridge: polls getUpdates, forwards to portal /api/chat, replies + TTS back."""
import json, os, re, sys, time, urllib.request, urllib.error

PORTAL = os.environ.get('PORTAL_BASE', 'http://127.0.0.1:8789').rstrip('/')

def tg(method, token, payload=None, timeout=90):
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(f'https://api.telegram.org/bot{token}/{method}',
        data=data if payload is not None else None,
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {'error': e.code, 'body': e.read().decode()[:200]}

def tg_file_url(token, file_path):
    return f'https://api.telegram.org/file/bot{token}/{file_path}'

def portal_chat(email, msg, token_store):
    req = urllib.request.Request(PORTAL + '/api/chat',
        data=json.dumps({'email': email, 'message': msg}).encode(),
        method='POST', headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token_store})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)

def portal_tts(email, text, token_store):
    req = urllib.request.Request(PORTAL + '/api/tts',
        data=json.dumps({'email': email, 'text': text}).encode(),
        method='POST', headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token_store})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()

def ogg_duration(path):
    import subprocess
    r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                        '-of', 'json', path], capture_output=True, text=True)
    try: return int(float(json.loads(r.stdout)['format']['duration']))
    except: return None

def send_voice(token, chat_id, audio_bytes, caption=''):
    import tempfile, subprocess
    with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as f:
        f.write(audio_bytes); tmp = f.name
    out = tmp.rsplit('.', 1)[0] + '.ogg'
    subprocess.run(['ffmpeg', '-y', '-i', tmp, '-c:a', 'libopus', '-b:a', '64k', out],
                   capture_output=True, timeout=30)
    boundary = '----ANOS7X'
    with open(out, 'rb') as f:
        media = f.read()
    fields = {'chat_id': str(chat_id)}
    if caption: fields['caption'] = caption
    body = b''
    for k, v in fields.items():
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    body += f'--{boundary}\r\nContent-Disposition: form-data; name="voice"; filename="voice.ogg"\r\nContent-Type: audio/ogg\r\n\r\n'.encode()
    body += media + f'\r\n--{boundary}--\r\n'.encode()
    req = urllib.request.Request(f'https://api.telegram.org/bot{token}/sendVoice',
        data=body, method='POST', headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)
    finally:
        os.unlink(tmp); os.unlink(out)

def main(email):
    base = os.environ.get('PORTAL_DIR', '/opt/data/portal')
    state_path = base + '/' + re.sub(r'[^a-z0-9@._-]', '', email) + '.telegram.json'
    token_path = base + '/customers.json'
    while True:
        try:
            state = json.load(open(state_path))
            token = state['token']
            db = json.load(open(token_path))
            cust = db['customers'].get(email, {})
            sess = cust.get('telegram_session_token')
            if not sess:
                # get or create a session token via portal internal endpoint
                req = urllib.request.Request(PORTAL + '/api/internal/session',
                    data=json.dumps({'email': email, 'admin': os.environ.get('PORTAL_ADMIN_SECRET', 'mammoth-admin')}).encode(),
                    method='POST', headers={'Content-Type': 'application/json'})
                try:
                    sess = json.load(urllib.request.urlopen(req, timeout=30))['token']
                except Exception as e:
                    print('no session yet:', e); time.sleep(5); continue
            last = state.get('offset', 0)
            upd = tg('getUpdates', token, {'offset': last + 1, 'timeout': 30})
            if 'result' not in upd:
                print('getUpdates err:', upd); time.sleep(5); continue
            for u in upd['result']:
                state['offset'] = u['update_id']
                msg = u.get('message') or {}
                chat_id = msg.get('chat', {}).get('id')
                if not chat_id:
                    continue
                text = msg.get('text', '').strip()
                voice = msg.get('voice')
                reply_text = None
                try:
                    if voice:
                        # fetch voice → transcribe via Hermes? Portal STT not built — ask user to type
                        pass
                    if text:
                        r = portal_chat(email, text, sess)
                        reply_text = r.get('reply', '')
                        # plain text reply
                        tg('sendMessage', token, {'chat_id': chat_id, 'text': reply_text or '…'})
                        # voice version
                        try:
                            cust2 = json.load(open(token_path))['customers'].get(email, {})
                            if cust2.get('voice'):
                                audio = portal_tts(email, reply_text[:300], sess)
                                send_voice(token, chat_id, audio)
                        except Exception as e:
                            print('tts fail:', e)
                except Exception as e:
                    print('portal err:', e)
            json.dump(state, open(state_path, 'w'))
        except Exception as e:
            print('loop err:', e)
        time.sleep(1)

if __name__ == '__main__':
    main(sys.argv[1])
