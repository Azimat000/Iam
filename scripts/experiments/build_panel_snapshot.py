"""Build the packaged read-only panel archive from published experiment evidence."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]


def build(root):
    base = Path(root) / 'docs/experiments'
    hashes = {}

    def read(name):
        data = (base / name).read_bytes()
        hashes[name] = hashlib.sha256(data).hexdigest()
        return data.decode('utf-8')

    def story(name, code):
        text = read(name)
        marks = list(re.finditer(r'^### Момент выбора (\d+)\s*$', text, re.M))
        if [int(m[1]) for m in marks] != list(range(1, 19)):
            raise ValueError('нужна полная опубликованная биография из 18 моментов')
        moments = []
        for index, mark in enumerate(marks):
            body = text[mark.end():marks[index + 1].start() if index < 17 else len(text)].strip()
            body = re.sub(r'\n## Сеанс \d+\s*$', '', body).strip()
            session = int(re.findall(r'^## Сеанс (\d+)\s*$', text[:mark.start()], re.M)[-1])
            moments.append({'number': index + 1, 'session': session, 'body': body})
        return {'id': code, 'moments': moments}

    matrix = json.loads(read('s16/live-memory-v2/summary.json'))
    development = json.loads(read('development/comparison-v2/summary.json'))
    agency = json.loads(read('development/agency-v1-results.json'))
    motivation_path = Path(root) / 'docs/evidence/S15-motivation-runs.csv'
    motivation_raw = motivation_path.read_bytes()
    hashes['../evidence/S15-motivation-runs.csv'] = hashlib.sha256(motivation_raw).hexdigest()
    motivations = list(csv.DictReader(motivation_raw.decode().splitlines()))
    biographies = [story('development/comparison-v2/blind/' + code + '.md', code)
                   for code in (f'D{number:03d}' for number in range(1, 13))]
    agency_stories = [story('development/agency-live-v1/full-biographies/' + row['cell'] + '.md', row['cell'])
                      for row in agency['rows']]
    return {'schema': 1, 'sources': hashes, 'series': [
        {'id': 'development', 'title': 'Развитие A/B/C',
         'description': 'Полная сравнительная серия: шесть сеансов, две истории обратной связи и два повтора. '
                        'Все слабые результаты и ошибки сохранены. Номера историй перемешаны.',
         'metrics': [['Биографий', development['cells']], ['Моментов выбора', 18 * development['cells']],
                     ['Контрольных ветвей', sum(len(row['probes']) for row in development['runs'])]],
         'findings': ['Все 12 биографий технически достоверны. Это допуск данных, а не положительная оценка личности.',
                      'Причинность и естественность рассматриваются поровну; убедительного победителя по обоим признакам нет.',
                      'Направление A выбрано для дальнейшей разработки. Человеческие баллы не назначены.'],
         'stories': biographies},
        {'id': 'matrix', 'title': 'Исходная матрица',
         'description': 'Восемь сценариев × три устройства × два повтора. A/B используют модель, C работает локально. '
                        'Сохранены непрошедшие условия; это историческая проверка отдельных механизмов.',
         'metrics': [['Ячеек', matrix['matrix_cells']], ['Выполнено', matrix['executed']], ['Вложенных прогонов', matrix['subruns']]],
         'findings': [f"Вариант {letter}: {value['technical_pass']} пройденных и {value['technical_fail']} непройденных условий билетов."
                      for letter, value in matrix['assessments'].items()] +
                     ['Непройденное условие билета не равно нарушению проверяющего ядра. Причинные пробелы сохранены.'],
         'stories': []},
        {'id': 'motivation', 'title': 'Мотивация',
         'description': 'S15: потребности, новизна и предложения целей сравнены на формальной основе C. '
                        'Предложения целей проверялись на заглушке ScriptedGoals.',
         'metrics': [['Прогонов', len(motivations)], ['Механизмов', len({row['mechanism'] for row in motivations})],
                     ['Живых вызовов модели', 0]],
         'findings': ['Проверены интерес, возврат к делу, потеря интереса, неудача, переключение и бездействие; есть контроль мотивации.',
                      'Для каждого механизма сохранено 28 прогонов. Все 10 обращений за целями были заглушечными.',
                      'Перенос свободных целей на A ещё требует проверки в S17. Превосходство на A этим сравнением не доказано.'],
         'stories': []},
        {'id': 'agency', 'title': 'Дальнейший вариант A',
         'description': 'Agency-v1: отдельная диагностика A без обязательного самоотчёта после каждого действия. '
                        'Это две новые истории, не повтор всей сравнительной матрицы.',
         'metrics': [['Историй', len(agency['rows'])], ['Моментов выбора', agency['main_choices']],
                     ['Внешних попыток модели', agency['external_attempts']]],
         'findings': ['Наблюдаются собственная работа и возвращение к ней. В одной истории есть рассказ с четырьмя версиями.',
                      'Речь остаётся рассудочной, встречаются повторы и содержательная ошибка; будущих сохранённых замыслов нет.',
                      '54 внешние попытки включают исправления формата и контрольные ветви. Технический успех не означает живость.'],
         'stories': agency_stories},
    ]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path, default=ROOT / 'src/iam/panel/experiments.json')
    args = parser.parse_args()
    data = json.dumps(build(args.root), ensure_ascii=False, indent=2) + '\n'
    if len(data.encode()) > 1_000_000:
        raise ValueError('снимок панели превышает допустимый размер')
    args.output.write_text(data)
    print('Снимок панели:', args.output)


if __name__ == '__main__':
    main()
