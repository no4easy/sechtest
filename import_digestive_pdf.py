"""Extract OCR text from the digestive pharmacology Moodle review PDF.

The file has one question per page and includes an explicit answer key. OCR
renders option labels inconsistently, so we locate options by their position
and check each selected option against the printed answer before importing.
"""
import json
import re
import subprocess
import sys
from difflib import SequenceMatcher
from pathlib import Path


def clean(value):
    value = re.sub(r'\s+', ' ', value).strip()
    value = re.sub(r'\s+[\\/%Х№Ж“”✓*]+$', '', value).strip()
    return re.sub(r'\s+вопрос$', '', value.replace(' вопрос ', ' '))


def key(value):
    return re.sub(r'[^\w]+', '', value.casefold()).replace('ё', 'е')


def extract(source):
    text = subprocess.check_output(['pdftotext', '-layout', str(source), '-']).decode('utf-8', 'replace')
    records = []
    for page in text.split('\f'):
        if not page.strip():
            continue
        heading = re.search(r'^Вопрос\s+(\S+)\s+(.+)$', page, re.M)
        answer_match = re.search(r'Правильный\s+ответ:', page)
        choose_match = re.search(r'Выберите\s+один\s+ответ:', page)
        if not heading or not answer_match or not choose_match:
            raise ValueError(f'Нераспознанная страница: {page[:100]!r}')
        number = len(records) + 1
        printed_number = heading.group(1).rstrip('.')
        if printed_number not in (str(number), {35: 'ЗБ', 142: '14:2'}.get(number)):
            raise ValueError(f'Вопрос {number}: необычный номер {printed_number!r}')
        question_lines = page[heading.end():choose_match.start()].splitlines()
        question = clean(' '.join([heading.group(2), *[re.sub(r'^(?:Верно|Неверно)\s*', '', x.strip()) for x in question_lines if x.strip() not in ('Верно', 'Неверно') and not x.startswith('Балл:') and not x.strip().startswith(('Отметить', 'Г Отметить'))]]))
        body = page[choose_match.end():answer_match.start()]
        answer = page[answer_match.end():]
        answer = clean(answer)
        options = []
        for line in body.splitlines():
            # OCR often reads б/г/д as Ь/Ч/4/Я/6. Order on the page is reliable.
            marker = re.search(r'(?:^|\s)[а-яa-z0-9ЬЧЯ][.]\s+', line, re.I)
            if marker and (line[:marker.start()].strip() in ('', 'Г Отметить', '7 Отметить', 'Отметить', 'ф Отметить', '| Отметить', '? Отметить', '{ Отметить', 'вопрос', 'Г Отметить', '№ Отметить') or 'Отметить' in line[:marker.start()]):
                options.append(clean(line[marker.end():]))
            elif options and line.strip() and line.strip() != 'вопрос':
                options[-1] = clean(options[-1] + ' ' + line.strip())
        if not 2 <= len(options) <= 6:
            raise ValueError(f'Вопрос {number}: извлечено {len(options)} вариантов: {options!r}')
        ratios = [SequenceMatcher(None, key(option), key(answer)).ratio() for option in options]
        selected = max(range(len(options)), key=ratios.__getitem__)
        if ratios[selected] < .89 or sorted(ratios)[-1] - sorted(ratios)[-2] < .08:
            raise ValueError(f'Вопрос {number}: ответ {answer!r} не подтвержден вариантами {options!r}; сходство {ratios!r}')
        records.append({'source_key': f'digestive-2024:{number}', 'subject': 'Фармакология · Средства для органов пищеварения', 'number': number, 'kind': 'single', 'question': question, 'options': options, 'answer': selected, 'answer_text': answer})
    return records


if __name__ == '__main__':
    records = extract(Path(sys.argv[1]))
    Path(sys.argv[2]).write_text(json.dumps(records, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'Saved {len(records)} questions')
