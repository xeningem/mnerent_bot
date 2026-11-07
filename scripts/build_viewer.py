"""Собирает docs/index.html: статическую страницу для GitHub Pages, в которую пользователь
загружает файл из /file. Файл читается только в браузере, никуда не отправляется.
Стили и скрипт берутся из core.py, поэтому страница не расходится с экспортом.
Использование (из корня репозитория): python scripts/build_viewer.py"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import EXPORT_CSS, EXPORT_JS  # noqa: E402

PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>Просмотр объявлений</title>
<style>__CSS__
.pick{border:2px dashed var(--line);border-radius:12px;padding:28px 16px;text-align:center;margin:24px 0}
.pick.over{border-color:var(--accent)}.pick input{display:none}
.pick label.btn{display:inline-block;background:var(--accent);color:var(--bg);border-radius:8px;padding:8px 16px;cursor:pointer}
.again{margin:8px 0}
</style></head><body><main>
<div id="start"><h1>Просмотр объявлений</h1>
<p class="mute">Откройте файл, который прислал бот командой /file. Он читается прямо в браузере и никуда не отправляется: поиск, сортировка и фильтры работают здесь.</p>
<div class="pick" id="drop"><label class="btn">Выбрать файл<input type="file" id="file" accept=".html,.htm,text/html"></label>
<p class="mute">или перетащите его сюда</p></div>
<p class="mute" id="err" hidden></p></div>
<div id="view" hidden><button class="again" id="again">Открыть другой файл</button><div id="doc"></div></div>
</main><script>
function show() {
__JS__
}
const $$ = id => document.getElementById(id);
function load(file) {
  if (!file) return;
  const reader = new FileReader();
  reader.onerror = () => fail('Не удалось прочитать файл.');
  reader.onload = () => {
    const src = new DOMParser().parseFromString(String(reader.result), 'text/html');   // разбор не выполняет скрипты
    const list = src.getElementById('list');
    if (!list || !list.querySelector('article')) return fail('В файле нет объявлений. Нужен файл, который прислал бот командой /file.');
    src.querySelectorAll('script,iframe,object,embed,link,meta').forEach(n => n.remove());
    src.querySelectorAll('*').forEach(n => [...n.attributes].forEach(a => { if (/^on/i.test(a.name)) n.removeAttribute(a.name); }));
    const doc = $$('doc');
    doc.replaceChildren();
    for (const node of src.querySelector('main').children) doc.append(document.importNode(node, true));
    $$('start').hidden = true; $$('view').hidden = false;
    show();
    window.scrollTo(0, 0);
  };
  reader.readAsText(file);
}
function fail(text) { $$('err').textContent = text; $$('err').hidden = false; }
$$('file').addEventListener('change', e => load(e.target.files[0]));
$$('again').onclick = () => { $$('view').hidden = true; $$('start').hidden = false; $$('file').value = ''; $$('err').hidden = true; };
const drop = $$('drop');
for (const t of ['dragenter', 'dragover']) drop.addEventListener(t, e => { e.preventDefault(); drop.classList.add('over'); });
for (const t of ['dragleave', 'drop']) drop.addEventListener(t, e => { e.preventDefault(); drop.classList.remove('over'); });
drop.addEventListener('drop', e => load(e.dataTransfer.files[0]));
</script></body></html>
"""

out = ROOT / 'docs' / 'index.html'
out.parent.mkdir(exist_ok=True)
out.write_text(PAGE.replace('__CSS__', EXPORT_CSS).replace('__JS__', EXPORT_JS), encoding='utf-8')
print('записано', out)
