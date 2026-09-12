const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const ts = require('typescript');
const seller = 'a1000000-0000-4000-8000-000000000001';
const account = 'b1000000-0000-4000-8000-000000000001';
const view = 'c1000000-0000-4000-8000-000000000001';
const jobId = 'd1000000-0000-4000-8000-000000000001';
function load(file, deps={}, context={}) {
  const exports={};
  const source=fs.readFileSync(path.join(__dirname,'../app/lib',file),'utf8');
  vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,
    {exports,require:name=>{if(!(name in deps))throw new Error(name);return deps[name];},URL,URLSearchParams,TextDecoder,AbortSignal,setTimeout,clearTimeout,...context});
  return exports;
}
const settings=load('seller-settings-types.ts');
const work=load('catalog-work-types.ts',{'./seller-settings-types':settings});
const types=load('publication-types.ts',{'./seller-settings-types':settings,'./catalog-work-types':work});
const index=()=>({seller_id:seller,can_manage:true,accounts:[{id:account,name:'Target',marketplace:'kaufland'}],views:[{id:view,name:'Vista',source_name:'Listino',row_count:1,revision:1,account_ids:[account],updated_at:'2026-09-12T12:00:00Z'}],jobs:[]});
const job=()=>({version:"a".repeat(64),id:jobId,seller_id:seller,account_id:account,marketplace:'kaufland',account_name:'Target',view_name:'Vista',status:'queued',total:1,filtered_total:1,counts:{pending:1},rules:{...types.defaultRules,account_id:account,view_id:view},created_at:'2026-09-12T12:00:00Z',rows:[]});
function proxy(fetch) {
  class NextResponse extends Response {static json(v,options){return new NextResponse(JSON.stringify(v),options);}}
  return load('publication-proxy.ts',{'next/server':{NextResponse},'./api-url':{apiUrl:'https://api.test'},'./seller-settings-types':settings,'./publication-types':types,'./catalog-proxy':{requestOrigin:r=>new URL(r.url).origin,boundedBody:async r=>new Uint8Array(await r.arrayBuffer())}},{fetch}).publicationProxy;
}
const post=(origin='https://app.test')=>new Request('https://app.test/api',{method:'POST',headers:{origin,cookie:'mh_session=test','content-type':'application/json'},body:JSON.stringify({confirmation:'PUBBLICA',selected:[]})});

test('publication DTO enforces Seller identity and strips credentials and private payloads',()=>{
  assert.equal(types.readPublicationJob({...job(),seller_id:account},seller),null);
  const result=types.readPublicationJob({...job(),credentials_encrypted:'secret',payload_json:'secret'},seller);
  assert.ok(result);assert.ok(!JSON.stringify(result).includes('secret'));
  assert.equal(types.readPublicationJob({...job(),status:'published'},seller),null);
  assert.equal(types.readPublicationIndex({...index(),jobs:[{...job(),seller_id:account}]},seller),null);
});
test('publication proxy refuses cross-origin submissions before any upstream call',async()=>{
  let calls=0;const fn=proxy(async()=>{calls++;throw new Error('must not fetch');});
  assert.equal((await fn(post('https://evil.test'),seller,['jobs',jobId,'submit'])).status,403);assert.equal(calls,0);
});
test('publication proxy checks write access before forwarding a body',async()=>{
  let calls=0;const fn=proxy(async()=>{calls++;return Response.json({...index(),can_manage:false});});
  assert.equal((await fn(post(),seller,['preview'])).status,403);assert.equal(calls,1);
});
test('ambiguous publication POST is never retried',async()=>{
  let calls=0;const fn=proxy(async(url,options)=>{calls++;if(options.method==='POST')throw new TypeError('network');return Response.json(index());});
  assert.equal((await fn(post(),seller,['jobs',jobId,'submit'])).status,503);assert.equal(calls,2);
});
test('metadata identity survives proxy sanitization and forged response is rejected',async()=>{
  const request=new Request('https://app.test/api?storefront=de&playground=true',{headers:{cookie:'mh_session=test'}});
  let bad=false;const fn=proxy(async()=>Response.json({seller_id:bad?account:seller,account_id:account,shipping_groups:[{id:'12',name:'Standard',credentials:'secret'}]}));
  const response=await fn(request,seller,['options',account]);assert.equal(response.status,200);
  const value=await response.json();assert.ok(types.readPublicationOptions(value,seller,account));assert.ok(!JSON.stringify(value).includes('secret'));
  bad=true;assert.equal((await fn(request,seller,['options',account])).status,502);
});
test('publication route is under the plug macro menu',()=>{
  const nav=load('seller-navigation.ts');
  const area=nav.sellerAreas.find(a=>a.id==='marketplaces');
  assert.equal(area.icon,'plug');
  assert.equal(area.sections.find(s=>s.page==='publication').href,'/seller/marketplaces/publish');
});


test('draft edits retain review version and pass only the scoped edit endpoint',async()=>{
  const calls=[];const fn=proxy(async(url,options)=>{calls.push({url,options});return Response.json(options.method==='POST'?{...job(),status:'draft'}:index());});
  const body={version:'a'.repeat(64),rows:[{id:jobId,price:'25.50'}]};
  const req=new Request('https://app.test/api',{method:'POST',headers:{origin:'https://app.test',cookie:'mh_session=test','content-type':'application/json'},body:JSON.stringify(body)});
  const res=await fn(req,seller,['jobs',jobId,'edit']);assert.equal(res.status,200);
  assert.equal(calls.length,2);assert.ok(calls[1].url.endsWith(`/sellers/${seller}/publication/jobs/${jobId}/edit`));
  assert.deepEqual(JSON.parse(calls[1].options.body),body);
  assert.equal((await res.json()).version,body.version);
  assert.equal(types.readPublicationJob({...job(),version:undefined},seller),null);
});


test('enriched product details survive both Seller and publication DTOs without source secrets',()=>{
 const info={brand:'Brand',description:'Product',token:'private'};
 const match={light:'matched',full:'matched',light_version:2,full_version:1,checked_at:'2026-09-12T12:00:00Z'};
 const row={id:view,ean:'123',sku:'SKU',name:'Named product',cost:'10.00',shipping_cost:'0',total_cost:'10',quantity:'7',price:'20',minimum_price:'15',weight_kg:'1.2',length_cm:'20',width_cm:'10',height_cm:'5',product_info:info,innpro_match:match};
 const viewData=work.readWorkData({seller_id:seller,page:1,total:1,rows:[row]},seller);
 assert.equal(viewData.rows[0].product_info.brand,'Brand');assert.ok(!JSON.stringify(viewData).includes('private'));
 const receipt=types.readPublicationJob({...job(),rows:[{...row,quantity:7,position:1,status:'pending',result_code:'',commission:'3',profit:'7',problem:''}]},seller);
 assert.equal(receipt.rows[0].length_cm,'20');assert.equal(receipt.rows[0].innpro_match.light,'matched');assert.equal(receipt.rows[0].name,'Named product');
});


test('all-products drafts exceed 100 and older range drafts remain compatible',()=>{
 const value={...job(),total:3363,filtered_total:3363,counts:{pending:3363}};
 assert.equal(types.readPublicationJob(value,seller).total,3363);
 assert.equal(types.readPublicationJob(value,seller).rules.selection_mode,'all');
 delete value.rules.selection_mode;
 assert.equal(types.readPublicationJob(value,seller).rules.selection_mode,'range');
 value.rules.selection_mode='invalid';
 assert.equal(types.readPublicationJob(value,seller),null);
});
