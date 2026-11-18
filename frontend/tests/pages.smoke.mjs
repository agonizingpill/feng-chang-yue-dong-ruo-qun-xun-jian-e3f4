// 前端页面冒烟：用 jsdom 执行各页 render/bind，捕获模板与事件绑定错误。
// 运行：node frontend/tests/pages.smoke.mjs  （需要 NODE_PATH 指向 jsdom）
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
let JSDOM;
try{JSDOM=require('jsdom').JSDOM;}catch{JSDOM=null;}
import {readFileSync,readdirSync} from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const root=path.resolve(import.meta.dirname,'..','src');
if(!JSDOM){console.error('需要 jsdom：npm i -D jsdom 或把 NODE_PATH 指向已安装目录');process.exit(2);}
const dom=new JSDOM('<!doctype html><div id="app"></div><div id="toast"></div>',{url:'http://localhost/#yard',pretendToBeVisual:true});
global.window=dom.window;global.document=dom.window.document;global.location=dom.window.location;
global.localStorage=dom.window.localStorage;global.FormData=dom.window.FormData;global.HTMLElement=dom.window.HTMLElement;
global.Node=dom.window.Node;global.fetch=async()=>({ok:true,json:async()=>[]});

const hives=[{id:1,code:'H-018',site:'山荆东坡',queen_present:true,colony_frames:6,care_state:'observe'},{id:2,code:'H-031',site:'山荆林缘',queen_present:false,colony_frames:4,care_state:'needs_care'}];
const visits=[
  {id:1,code:'V-1',hive_id:1,weight_kg:24,temperature:31,activity:40,feed_left:1,queen_seen:true,colony_frames:6,status:'screened',intake_state:'current',collected_at:'2026-11-17T02:00:00',recorded_at:'2026-11-17T02:05:00',anomaly_score:-.4,anomaly_flag:false,model_evidence:{threshold:-.5}},
  {id:2,code:'V-OLD',hive_id:1,weight_kg:21,temperature:30,activity:20,feed_left:.4,queen_seen:true,colony_frames:5,status:'screened',intake_state:'late',collected_at:'2026-11-16T02:00:00',recorded_at:'2026-11-17T03:00:00'},
  {id:3,code:'V-C',hive_id:2,weight_kg:22,temperature:30,activity:18,feed_left:.2,queen_seen:false,colony_frames:4,status:'conflict',intake_state:'conflict',collected_at:'2026-11-17T04:00:00',recorded_at:'2026-11-17T04:05:00'},
];
const conflict={id:9,hive_id:1,collected_at:'2026-11-17T02:00:00',state:'open',readings:[{visit_id:1,code:'V-1',weight_kg:24,temperature:31,activity:40,feed_left:1,queen_seen:true,colony_frames:6}],resolved_visit_id:null,decided_by:'',decided_at:null,note:''};
const batch={batch_id:'OFF-1',stored_id:1,duplicate:false,state:'applied',submitted_by:'beekeeper@shanjing.test',submitted_at:'2026-11-17T05:00:00',results:[{client_record_id:'V-1',code:'V-1',outcome:'accepted',visit_id:1,conflict_id:null,intake_state:'current',message:'已接纳为当前箱况'}],summary:{accepted:1,late:0,deferred:0,conflict:0,duplicate:0,rejected:0}};

const state={hives,visits,care:[{id:1,visit_id:1,kind:'feed',feed_kg:1.5,status:'planned',queen_installed:false,followup_weight:null,followup_queen:null,followup_frames:null,executed_at:null,closed_at:null}],lots:[],issues:[],queens:[],moves:[],insulationLots:[],installs:[],spring:[],batches:[batch],conflicts:[conflict]};

global.localStorage.setItem('apiary-account',JSON.stringify({token:'t',email:'beekeeper@shanjing.test',role:'beekeeper'}));
const notebook=await import(pathToFileURL(path.join(root,'notebook.js')));
Object.assign(notebook.book,state);

const pages=readdirSync(path.join(root,'pages')).filter(f=>f.endsWith('.js'));
const failures=[];
for(const file of pages){
  const name=file.replace('.js','');
  try{
    const page=await import(pathToFileURL(path.join(root,'pages',file)));
    const html=page.render();
    if(typeof html!=='string'||!html.trim())throw new Error('render 返回空');
    document.querySelector('#app').innerHTML=`<main>${html}</main>`;
    page.bind(()=>{});
    console.log('ok',name,html.length);
  }catch(error){failures.push([name,error]);console.log('FAIL',name,error.message);}
}
if(failures.length){console.error(failures.map(([n,e])=>n+': '+e.stack).join('\n'));process.exit(1);}
