import {api} from './api.js';
export const book={hives:[],visits:[],care:[],lots:[],issues:[],queens:[],moves:[],insulationLots:[],installs:[],spring:[],batches:[],conflicts:[],selectedHive:31,selectedVisit:null,selectedCare:null,planning:false,showAll:false,trace:null};
export async function openNotebook(){[book.hives,book.visits,book.care,book.lots,book.issues,book.queens,book.moves,book.insulationLots,book.installs,book.spring,book.batches,book.conflicts]=await Promise.all([api('/hives'),api('/visits'),api('/care'),api('/feed-lots'),api('/feed-issues'),api('/queen-observations'),api('/moves'),api('/insulation-lots'),api('/insulation-installs'),api('/spring-reviews'),api('/visit-batches'),api('/visit-conflicts')]);}
export const stamp=v=>v.collected_at||v.recorded_at;
export const isCurrent=v=>v.intake_state==='current'||v.intake_state==='accepted'||v.intake_state==null;
export function currentVisit(hiveId){let best=null;for(const v of book.visits){if(v.hive_id!==hiveId||!isCurrent(v))continue;if(!best||(stamp(v)||'')>(stamp(best)||'')||((stamp(v)||'')===(stamp(best)||'')&&v.id>best.id))best=v;}return best;}
export function latestVisits(){return book.hives.map(h=>currentVisit(h.id)).filter(Boolean);}
export const openConflicts=()=>book.conflicts.filter(c=>c.state==='open');
