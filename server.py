import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).parent
DB = Path(os.environ.get('DATABASE_PATH', ROOT / 'study.sqlite3'))
ADMIN_TOKEN = os.environ.get('ADMIN_TOKEN', '')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')
PORT = int(os.environ.get('PORT', '8080'))


def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB)
    db.execute('PRAGMA busy_timeout=5000')
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE IF NOT EXISTS questions (id INTEGER PRIMARY KEY, subject TEXT NOT NULL, question TEXT NOT NULL, options TEXT NOT NULL, answer INTEGER NOT NULL, explanation TEXT NOT NULL DEFAULT "", UNIQUE(subject, question))')
    db.execute('CREATE TABLE IF NOT EXISTS passages (id INTEGER PRIMARY KEY, subject TEXT NOT NULL, text TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS seed_questions (source_key TEXT PRIMARY KEY, subject TEXT NOT NULL, kind TEXT NOT NULL, question TEXT NOT NULL, options TEXT NOT NULL, answer INTEGER NOT NULL, answer_text TEXT NOT NULL)')
    db.commit()
    return db


def import_seed():
    with connect() as db:
        for seed_file in sorted(ROOT.glob('seed_*.json')):
            items = json.loads(seed_file.read_text(encoding='utf-8'))
            db.executemany('INSERT INTO seed_questions(source_key, subject, kind, question, options, answer, answer_text) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(source_key) DO UPDATE SET subject=excluded.subject, kind=excluded.kind, question=excluded.question, options=excluded.options, answer=excluded.answer, answer_text=excluded.answer_text',
                [(x['source_key'], x['subject'], x['kind'], x['question'], json.dumps(x['options'], ensure_ascii=False), x['answer'], x['answer_text']) for x in items])
        db.commit()


def normalize(text):
    return re.sub(r'\s+', ' ', text).strip()


def extract_questions(text):
    # Common PDF layout: numbered question, lettered answers, explicit answer key.
    chunks = re.split(r'(?m)^\s*(?:Вопрос\s*)?(\d{1,4})[.)]\s+', '\n' + text)
    found = []
    for i in range(1, len(chunks) - 1, 2):
        body = chunks[i + 1]
        parts = re.split(r'(?m)^\s*([А-ГA-D])[.)]\s+', body)
        if len(parts) < 5:
            continue
        question = normalize(parts[0])
        opts = []
        for j in range(1, len(parts) - 1, 2):
            opts.append((parts[j].upper(), normalize(parts[j + 1])))
        if not 2 <= len(opts) <= 6 or not question:
            continue
        answer_match = re.search(r'(?:Правильный\s+ответ|Ответ)\s*[:—-]\s*([А-ГA-D])\b', body, re.I)
        if not answer_match:
            continue
        answer_letter = answer_match.group(1).upper()
        answer = next((n for n, (letter, _) in enumerate(opts) if letter == answer_letter), None)
        if answer is None:
            continue
        clean = []
        for _, option in opts:
            option = re.split(r'(?:Правильный\s+ответ|Ответ)\s*[:—-]', option, flags=re.I)[0].strip()
            clean.append(option)
        if all(clean):
            found.append((question, clean, answer))
    return found


def ai_answer(question, passages):
    if not OPENAI_API_KEY:
        return None
    context = '\n\n'.join(p['text'][:2500] for p in passages[:5])[:10000]
    prompt = ('Ответь на вопрос студента по-русски. Используй фрагменты учебных материалов. '
              'Если в материалах нет ответа, явно скажи об этом, затем дай осторожный ответ на основе общих знаний. '
              'Не выдумывай номер страницы или цитату.\n\nМатериалы:\n' + context + '\n\nВопрос: ' + question)
    payload = json.dumps({'model': os.environ.get('OPENAI_MODEL', 'gpt-4.1-mini'), 'input': prompt, 'max_output_tokens': 450}).encode()
    req = urllib.request.Request('https://api.openai.com/v1/responses', payload,
        {'Authorization': 'Bearer ' + OPENAI_API_KEY, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as response:
        data = json.load(response)
    return ' '.join(item.get('text', '') for out in data.get('output', []) for item in out.get('content', []) if item.get('type') == 'output_text').strip()


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self, limit=15_000_000):
        size = int(self.headers.get('Content-Length', '0'))
        if size < 1 or size > limit:
            raise ValueError('Неверный размер запроса')
        return json.loads(self.rfile.read(size))

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == '/health':
            return self.send_json(200, {'status': 'ok'})
        if url.path == '/':
            body = (ROOT / 'index.html').read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if url.path == '/api/subjects':
            with connect() as db:
                rows = db.execute('SELECT subject, COUNT(*) count FROM (SELECT subject FROM questions UNION ALL SELECT subject FROM seed_questions) GROUP BY subject ORDER BY subject').fetchall()
            return self.send_json(200, {'subjects': [dict(r) for r in rows]})
        if url.path == '/api/questions':
            args = parse_qs(url.query)
            subject = (args.get('subject') or [''])[0][:100]
            if not subject:
                return self.send_json(400, {'error': 'Выберите раздел теста'})
            with connect() as db:
                rows = db.execute('SELECT * FROM (SELECT CAST(id AS TEXT) id, subject, question, options, "single" kind FROM questions WHERE subject = ? UNION ALL SELECT source_key id, subject, question, options, kind FROM seed_questions WHERE subject = ?) ORDER BY RANDOM() LIMIT 20', (subject, subject)).fetchall()
            return self.send_json(200, {'questions': [{**dict(r), 'options': json.loads(r['options'])} for r in rows]})
        self.send_json(404, {'error': 'Не найдено'})

    def do_POST(self):
        try:
            path = urlparse(self.path).path
            if path == '/api/admin/upload':
                if not ADMIN_TOKEN or not hmac.compare_digest(self.headers.get('X-Admin-Token', ''), ADMIN_TOKEN):
                    return self.send_json(403, {'error': 'Неверный код администратора'})
                data = self.read_json()
                subject = normalize(str(data.get('subject', '')))[:100]
                raw = base64.b64decode(data.get('pdf', ''), validate=True)
                if not subject or not raw.startswith(b'%PDF-') or len(raw) > 10_000_000:
                    raise ValueError('Нужен PDF до 10 МБ и название предмета')
                with tempfile.TemporaryDirectory() as tmp:
                    source = Path(tmp) / 'source.pdf'
                    source.write_bytes(raw)
                    result = subprocess.run(['pdftotext', '-layout', str(source), '-'], capture_output=True, timeout=25)
                if result.returncode:
                    raise ValueError('Не удалось прочитать PDF; для сканов нужен OCR')
                content = result.stdout.decode('utf-8', errors='replace')
                if len(normalize(content)) < 30:
                    raise ValueError('В PDF нет распознаваемого текста; для сканов нужен OCR')
                questions = extract_questions(content)
                passages = [normalize(content[i:i + 1700]) for i in range(0, min(len(content), 250000), 1500)]
                with connect() as db:
                    inserted = 0
                    for q, opts, ans in questions:
                        cursor = db.execute('INSERT OR IGNORE INTO questions(subject, question, options, answer) VALUES (?, ?, ?, ?)', (subject, q, json.dumps(opts, ensure_ascii=False), ans))
                        inserted += cursor.rowcount
                    db.executemany('INSERT INTO passages(subject, text) VALUES (?, ?)', [(subject, p) for p in passages if p])
                    db.commit()
                return self.send_json(200, {'questions_added': inserted, 'passages_added': len(passages)})
            if path == '/api/answer':
                data = self.read_json(10000)
                question = normalize(str(data.get('question', '')))[:1000]
                subject = normalize(str(data.get('subject', '')))[:100]
                if len(question) < 3:
                    raise ValueError('Введите вопрос')
                if not subject:
                    raise ValueError('Выберите раздел')
                with connect() as db:
                    rows = db.execute('SELECT * FROM questions WHERE subject = ?', (subject,)).fetchall()
                    seed_rows = db.execute('SELECT * FROM seed_questions WHERE subject = ?', (subject,)).fetchall()
                    words = set(re.findall(r'\w{3,}', question.casefold()))
                    ranked = sorted([*rows, *seed_rows], key=lambda r: len(words & set(re.findall(r'\w{3,}', r['question'].casefold()))) / max(len(words), 1), reverse=True)
                    match = ranked[0] if ranked else None
                    score = len(words & set(re.findall(r'\w{3,}', match['question'].casefold()))) / max(len(words), 1) if match else 0
                    passages = db.execute('SELECT subject, text FROM passages WHERE subject = ?', (subject,)).fetchall()
                    passages = sorted(passages, key=lambda p: len(words & set(re.findall(r'\w{3,}', p['text'].casefold()))), reverse=True)[:5]
                if match and score >= .55:
                    options = json.loads(match['options'])
                    answer = match['answer_text'] if 'answer_text' in match.keys() else options[match['answer']]
                    return self.send_json(200, {'type': 'database', 'answer': answer, 'question': match['question'], 'subject': match['subject'], 'score': round(score, 2)})
                if not passages:
                    return self.send_json(200, {'type': 'none', 'answer': 'В базе пока нет подходящих материалов.'})
                answer = ai_answer(question, passages)
                if answer:
                    return self.send_json(200, {'type': 'ai', 'answer': answer, 'subjects': list(dict.fromkeys(p['subject'] for p in passages))})
                return self.send_json(200, {'type': 'excerpt', 'answer': passages[0]['text'][:650], 'subject': passages[0]['subject']})
            if path == '/api/check':
                data = self.read_json(2000)
                with connect() as db:
                    item_id = str(data.get('id', ''))
                    if ':' in item_id:
                        row = db.execute('SELECT answer, answer_text, kind FROM seed_questions WHERE source_key = ?', (item_id,)).fetchone()
                    else:
                        row = db.execute('SELECT answer, explanation FROM questions WHERE id = ?', (item_id,)).fetchone()
                if not row:
                    return self.send_json(404, {'error': 'Вопрос не найден'})
                return self.send_json(200, {'correct': data.get('answer') == row['answer'], 'answer': row['answer'], 'explanation': row['answer_text'] if 'answer_text' in row.keys() else row['explanation']})
            self.send_json(404, {'error': 'Не найдено'})
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            self.send_json(400, {'error': str(e)})
        except (urllib.error.URLError, TimeoutError):
            self.send_json(502, {'error': 'ИИ сейчас недоступен. Попробуйте ещё раз.'})
        except Exception as e:
            print('Request error:', repr(e))
            self.send_json(500, {'error': 'Ошибка сервера'})


if __name__ == '__main__':
    import_seed()
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
