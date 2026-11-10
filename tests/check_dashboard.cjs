// Проверка исполнения интерфейса с настоящими API-данными и минимальной DOM-моделью.
// Запуск при работающей панели: node tests/check_dashboard.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

class Element {
  constructor(tag) { this.tagName=tag; this.children=[]; this.style={}; this.dataset={}; this.listeners={}; this._text=''; }
  append(...children) { this.children.push(...children); } // DOM append() возвращает undefined.
  replaceChildren(...children) { this.children=[]; this._text=''; this.append(...children); }
  addEventListener(name, handler) { this.listeners[name]=handler; }
  get firstChild() { return this.children[0] || null; }
  set textContent(value) { this._text=String(value); this.children=[]; }
  get textContent() { return this._text+this.children.map(x=>typeof x==='string'?x:x.textContent).join(''); }
  querySelectorAll(selector) {
    const found=[];
    for (const child of this.children) if (child instanceof Element) {
      if (selector==='details[open]' && child.tagName==='details' && child.open) found.push(child);
      found.push(...child.querySelectorAll(selector));
    }
    return found;
  }
}

(async()=>{
  const elements=Object.fromEntries(['cards','traces','detail','alerts'].map(id=>[id,new Element('div')]));
  const html=fs.readFileSync(path.join(__dirname,'..','web','telemetry_dashboard.html'),'utf8');
  const source=html.split('<script>')[1].split('</script>')[0].replace('refresh();setInterval(refresh,5000);','');
  const errors=[];
  const context=vm.createContext({document:{getElementById:id=>elements[id],createElement:tag=>new Element(tag)},
    fetch:url=>fetch('http://127.0.0.1:18736'+url),console:{error:(...args)=>errors.push(args)},setInterval(){},URLSearchParams,location:{search:''}});
  vm.runInContext(source,context);
  await vm.runInContext('refresh()',context);
  assert.equal(errors.length,0,'Интерфейс завершился ошибкой');
  assert.ok(elements.cards.children.length>0,'Карточки не отображены');
  const rows=await fetch('http://127.0.0.1:18736/api/traces').then(r=>r.json());
  if(rows.length){
    const table=elements.traces.firstChild;
    assert.equal(table.tagName,'table');
    assert.equal(table.children[0].tagName,'thead');
    assert.equal(table.children[1].children.length,rows.length,'Запросы отсутствуют в таблице');
    await vm.runInContext(`loadDetail(${JSON.stringify(rows[0].run_id)})`,context);
    assert.ok(elements.detail.textContent.includes(rows[0].run_id),'Детали запроса не загрузились');
    assert.equal(errors.length,0);
  }
  vm.runInContext('renderTraces=()=>{throw new Error("render-test")}',context);
  await vm.runInContext('refresh()',context);
  assert.ok(elements.cards.textContent.includes('Ошибка отображения панели'),'Ошибка JS ошибочно названа сетевым сбоем');
  console.log(`Пройдено: карточки, таблица (${rows.length} запросов), детали и различение ошибок API/JavaScript.`);
})().catch(error=>{console.error(error);process.exitCode=1});
