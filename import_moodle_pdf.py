"""Build a reviewable seed JSON from a Moodle review PDF with answer keys."""
import json
import re
import subprocess
import sys
from difflib import SequenceMatcher
from pathlib import Path


def clean(value):
    value = re.sub(r'Нет ответа|Балл:\s*\d+|Баллов:\s*\d+\s+из\s+\d+|Верно|Неверно|Частично верно', ' ', value)
    return re.sub(r'\s+', ' ', value).strip().replace('ĸ', 'к').replace('', '')


def key(value):
    return re.sub(r'[^\w]+', '', clean(value).casefold()).replace('ё', 'е')


def extract(source, subject, source_id):
    text = subprocess.check_output(['pdftotext', '-layout', str(source), '-']).decode('utf-8', errors='replace')
    chunks = re.split(r'(?m)^\s*Вопрос\s+(\d+)\s+', text)
    if len(chunks) < 3:
        raise ValueError('Вопросы не найдены')
    records = []
    for i in range(1, len(chunks) - 1, 2):
        number = int(chunks[i])
        block = chunks[i + 1]
        if block.count('Правильный ответ:') == 0:
            print(f'Пропуск вопроса {number}: в PDF не указан правильный ответ', file=sys.stderr)
            continue
        if block.count('Правильный ответ:') != 1:
            raise ValueError(f'Вопрос {number}: найдено несколько блоков с ответом')
        body, raw_answer = block.split('Правильный ответ:', 1)
        answer_text = clean(raw_answer.split('\f', 1)[0])
        # The review page can end with a new test header after the answer.
        answer_text = re.split(r'Личный кабинет\s*/|Тест начат\s+|◀', answer_text)[0].strip()
        single = 'Выберите один ответ:' in body
        if single:
            stem, variants = body.split('Выберите один ответ:', 1)
            option_chunks = re.split(r'(?m)^\s*([a-f])[.]\s+', variants)
            options = [clean(option_chunks[j + 1]) for j in range(1, len(option_chunks) - 1, 2)]
            if not 2 <= len(options) <= 6:
                raise ValueError(f'Вопрос {number}: найдено {len(options)} вариантов')
            ratios = [SequenceMatcher(None, key(x), key(answer_text)).ratio() for x in options]
            answer_index = max(range(len(options)), key=ratios.__getitem__)
            if ratios[answer_index] < 0.82:
                raise ValueError(f'Вопрос {number}: ответ не совпадает с вариантами: {answer_text!r}')
            kind = 'single'
            question = clean(stem)
        else:
            kind = 'ordering' if 'Расположите' in body else 'matching' if 'Соотнесите' in body else 'reference'
            question = clean(body).replace('Выберите...', '')
            options = []
            answer_index = -1
        if not question or not answer_text:
            raise ValueError(f'Вопрос {number}: пустой вопрос или ответ')
        records.append({'source_key': f'{source_id}:{number}', 'subject': subject,
                        'number': number, 'kind': kind, 'question': question,
                        'options': options, 'answer': answer_index, 'answer_text': answer_text})
    return records


if __name__ == '__main__':
    if len(sys.argv) != 5:
        raise SystemExit('Usage: python import_moodle_pdf.py INPUT.pdf SUBJECT SOURCE_ID OUTPUT.json')
    records = extract(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
    Path(sys.argv[4]).write_text(json.dumps(records, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'Saved {len(records)} questions')
