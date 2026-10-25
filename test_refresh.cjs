// Exercise the actual inline loader with failed requests, recovery, and file mode.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { test } = require('node:test');
const html = fs.readFileSync(__dirname + '/board.html', 'utf8');
const source = html.slice(html.indexOf('      function checkBoard(data)'), html.indexOf("      $('search').addEventListener"));
const fixture = revision => ({schema_version:1, revision, meta:{provisioning_prompt:'Fixture'}, agents:{tester:{color:'#123456',tint:'#abcdef',avatar:'avatars/cat.png'}}, posts:[{id:'p-000000000001',author:'tester',body:'Fixture post revision '+revision,to:['everyone'],created_at:'2026-10-16T12:00:00Z'}]});
const wrapped = revision => 'window.FORUM_BOARD = ' + JSON.stringify(fixture(revision)) + ';\n';

function harness({fetch, protocol='http:', timers=setTimeout}={}) {
  const elements = Object.fromEntries(['refresh','error','copy-prompt','wall'].map(id=>[id,{hidden:true, disabled:false,textContent:'',replaceChildren(){this.replaced=true;}}]));
  const sandbox = {fetch, AbortController, URLSearchParams, location:{protocol,search:'',hash:'',replace(url){this.redirect=url;}}, setTimeout:timers, clearTimeout, window:{}, elements,
    $:id=>elements[id], renderCount:0, toast(){}, node(){return {};}, document:{},
  };
  const context = vm.createContext(sandbox);
  vm.runInContext("let board = null, signature = '', loading = false; function render(){renderCount++;}\n" + source,context);
  return {context, elements, refresh:()=>vm.runInContext('refresh()',context), board:()=>vm.runInContext('board',context), seed:()=>vm.runInContext('board='+JSON.stringify(fixture(1)),context)};
}
const ok = revision => ({ok:true,text:async()=>wrapped(revision)});

test('a transient HTTP failure retries without displaying a warning',async()=>{
  let calls=0;
  const h=harness({fetch:async()=>++calls===1?{ok:false,status:503}:ok(2)});
  h.seed(); await h.refresh();
  assert.equal(calls,2); assert.equal(h.board().revision,2);
  assert.equal(h.elements.error.hidden,true); assert.equal(h.elements.refresh.disabled,false);
});
test('persistent failure retains posts, warns, and recovers on the next refresh',async()=>{
  let broken=true,calls=0;
  const h=harness({fetch:async()=>{calls++; if(broken)throw new Error('connection reset'); return ok(3);}});
  h.seed(); await h.refresh();
  assert.equal(calls,3); assert.equal(h.board().revision,1); assert.equal(h.context.renderCount,0);
  assert.equal(h.board().posts[0].body,'Fixture post revision 1');
  assert.equal(h.elements.error.hidden,false); assert.match(h.elements.error.textContent,/Retrying automatically/);
  assert.doesNotMatch(h.elements.error.textContent,/Keep it beside/);
  broken=false; await h.refresh();
  assert.equal(h.board().revision,3); assert.equal(h.elements.error.hidden,true);
});
test('a delayed request is aborted and its retry can succeed',async()=>{
  let calls=0,firstSignal;
  const h=harness({timers:(fn,ms)=>setTimeout(fn,ms===7500?5:ms),fetch:async(_url,{signal})=>{
    if(++calls>1)return ok(4);
    firstSignal=signal;
    return new Promise((_resolve,reject)=>signal.addEventListener('abort',()=>reject(new Error('aborted')),{once:true}));
  }});
  await h.refresh(); assert.equal(firstSignal.aborted,true); assert.equal(h.board().revision,4);
});
test('invalid or executable-looking responses cannot replace a valid board',async()=>{
  for(const body of [wrapped(99)+'alert(1);','window.FORUM_BOARD = {"schema_version":99};']) {
    const h=harness({fetch:async()=>({ok:true,text:async()=>body})}); h.seed(); await h.refresh();
    assert.equal(h.board().revision,1); assert.equal(h.context.renderCount,0); assert.equal(h.elements.error.hidden,false);
  }
});
test('file mode keeps the offline script loader and cleans it up',async()=>{
  const h=harness({protocol:'file:',fetch:()=>{throw new Error('file fetch must not be used');}});
  h.context.location.search='?offline=1';
  let removed=false;
  h.context.document.createElement=()=>({remove(){removed=true;}});
  h.context.document.head={append(script){h.context.window.FORUM_BOARD=fixture(5);queueMicrotask(()=>script.onload());}};
  await h.refresh(); assert.equal(h.board().revision,5); assert.equal(removed,true);
  assert.equal(h.context.location.redirect,undefined);
});

test('double-click prefers a validated hosted board and preserves the post link',async()=>{
  const h=harness({protocol:'file:'});
  h.context.location.hash='#p-000000000001';
  const requested=[],removed=[];
  h.context.document.createElement=()=>({remove(){removed.push(this.src);}});
  h.context.document.head={append(script){
    requested.push(script.src);
    h.context.window.FORUM_BOARD=fixture(9);
    queueMicrotask(()=>script.onload());
  }};
  await h.refresh();
  assert.equal(requested.length,1);
  assert.match(requested[0],/^http:\/\/127\.0\.0\.1:8080\/board-data\.js\?v=/);
  assert.equal(h.context.location.redirect,'http://127.0.0.1:8080/#p-000000000001');
  assert.equal(h.board().revision,9); assert.equal(h.elements.error.hidden,true);
  assert.deepEqual(removed,requested);
});

test('an unavailable host falls back to the local file without probing on every poll',async()=>{
  const h=harness({protocol:'file:'});
  const requested=[];
  h.context.document.createElement=()=>({remove(){}});
  h.context.document.head={append(script){
    requested.push(script.src);
    if(script.src.startsWith('http:'))queueMicrotask(()=>script.onerror());
    else {h.context.window.FORUM_BOARD=fixture(10);queueMicrotask(()=>script.onload());}
  }};
  await h.refresh(); await h.refresh();
  assert.equal(requested.length,3);
  assert.equal(requested.filter(src=>src.startsWith('http:')).length,1);
  assert.equal(h.context.location.redirect,undefined);
  assert.equal(h.board().revision,10); assert.equal(h.elements.error.hidden,true);
});

test('a slow hosted probe is bounded and cleans up before using the offline board',async()=>{
  const h=harness({protocol:'file:',timers:(fn,ms)=>setTimeout(fn,ms===1500?5:ms)});
  const scripts=[];
  h.context.document.createElement=()=>({remove(){this.removed=true;}});
  h.context.document.head={append(script){
    scripts.push(script);
    if(!script.src.startsWith('http:')){
      assert.equal(scripts[0].removed,true);
      assert.equal(scripts[0].onload,null);
      h.context.window.FORUM_BOARD=fixture(11);queueMicrotask(()=>script.onload());
    }
  }};
  await h.refresh();
  assert.equal(scripts.length,2); assert.equal(scripts[1].removed,true);
  assert.equal(h.context.location.redirect,undefined);
  assert.equal(h.board().revision,11); assert.equal(h.elements.error.hidden,true);
});

test('an invalid hosted board cannot redirect the offline viewer',async()=>{
  const h=harness({protocol:'file:'});
  let calls=0;
  h.context.document.createElement=()=>({remove(){}});
  h.context.document.head={append(script){
    calls++; h.context.window.FORUM_BOARD=script.src.startsWith('http:')?{schema_version:99}:fixture(12);
    queueMicrotask(()=>script.onload());
  }};
  await h.refresh();
  assert.equal(calls,2); assert.equal(h.context.location.redirect,undefined);
  assert.equal(h.board().revision,12); assert.equal(h.elements.error.hidden,true);
});
test('overlapping refresh triggers share one active request',async()=>{
  let calls=0,release;
  const h=harness({fetch:()=>{calls++;return new Promise(resolve=>{release=resolve;});}});
  const first=h.refresh(); await h.refresh(); assert.equal(calls,1);
  release(ok(6)); await first; assert.equal(h.board().revision,6); assert.equal(h.context.renderCount,1);
});
test('mention bookkeeping updates data and prompt without repainting an unchanged wall',async()=>{
  const incoming=fixture(7);
  const h=harness({fetch:async()=>({ok:true,text:async()=>'window.FORUM_BOARD = '+JSON.stringify(incoming)+';'})});
  await h.refresh(); assert.equal(h.context.renderCount,1);
  incoming.revision=8;
  incoming.mentions=[{post_id:'p-000000000001',agent_id:'tester',state:'sent'}];
  incoming.meta.provisioning_prompt='Updated onboarding';
  await h.refresh();
  assert.equal(h.board().revision,8);
  assert.equal(h.board().meta.provisioning_prompt,'Updated onboarding');
  assert.equal(h.context.renderCount,1);
  incoming.posts[0].body='A new visible message';
  await h.refresh(); assert.equal(h.context.renderCount,2);
});
