import { isUuid } from "./seller-settings-types";
import { readProductInfo, readInnproMatch, type ProductInfo, type InnproMatch, readWorkIndex, type WorkIndex } from "./catalog-work-types";

export type PublicationRules = {account_id:string; view_id:string; revision:number; storefront:string; playground:boolean; margin:number; minimum_margin:number; commission:number; min_qty:number; min_cost:number; min_profit:number; weight_mode:string; weight_from:number; weight_to:number; composite_sku:boolean; shipping_group:string; warehouse:string; handling:number; vat:string; multiplier:number; fx_date:string; logistic_class:string; state_code:string; ship_from:string; start:number; limit:number};
export type PublicationRow = {product_info?:ProductInfo; innpro_match?:InnproMatch; length_cm?:string|null; width_cm?:string|null; height_cm?:string|null;id:string; position:number; status:string; result_code:string; name:string; ean:string; sku:string; quantity:number; cost:string; price:string; minimum_price:string; commission:string; profit:string; weight_kg:string|null; problem:string};
export type PublicationJob = {version:string; id:string; seller_id:string; account_id:string; marketplace:string; account_name:string; view_name:string; status:string; total:number; filtered_total:number; counts:Record<string,number>; rules:PublicationRules; created_at:string; rows:PublicationRow[]};
export type PublicationIndex = WorkIndex & {jobs:PublicationJob[]};
export type Options = Record<string,{id:string;name:string}[]>;
export const defaultRules:PublicationRules = {account_id:"",view_id:"",revision:1,storefront:"de",playground:true,margin:35,minimum_margin:10,commission:15,min_qty:1,min_cost:0,min_profit:0,weight_mode:"none",weight_from:0,weight_to:0,composite_sku:false,shipping_group:"",warehouse:"",handling:1,vat:"standard_rate",multiplier:1,fx_date:"",logistic_class:"",state_code:"11",ship_from:"IT|Italy",start:1,limit:100};
const object=(v:unknown):v is Record<string,unknown>=>!!v&&typeof v==="object"&&!Array.isArray(v);
const str=(v:unknown):v is string=>typeof v==="string"&&v.length<=10000;
const count=(v:unknown):v is number=>typeof v==="number"&&Number.isSafeInteger(v)&&v>=0;
const statuses=["draft","queued","running","interrupted","finished"];
const rowStatuses=["invalid","pending","sending","accepted","submitted","rejected","unknown","skipped"];
export function readPublicationJob(v:unknown,seller:string):PublicationJob|null {
  if(!object(v)||typeof v.version!=="string"||!/^[a-f0-9]{64}$/.test(v.version)||!isUuid(v.id)||v.seller_id!==seller||!isUuid(v.account_id)||!str(v.marketplace)||!str(v.account_name)||!str(v.view_name)||!str(v.status)||!statuses.includes(v.status)||!count(v.total)||v.total>100||!count(v.filtered_total)||!object(v.counts)||!object(v.rules)||!str(v.created_at)||!Array.isArray(v.rows)||v.rows.length>100)return null;
  const counts:Record<string,number>={};for(const [k,n] of Object.entries(v.counts)){if(!rowStatuses.includes(k)||!count(n))return null;counts[k]=n;}
  const rules={...defaultRules};for(const k of Object.keys(defaultRules) as (keyof PublicationRules)[]){const val=v.rules[k];if(typeof val!==typeof defaultRules[k]||(typeof val==="number"&&!Number.isFinite(val))||(typeof val==="string"&&val.length>200))return null;Object.assign(rules,{[k]:val});}
  if(rules.account_id!==v.account_id||!isUuid(rules.view_id))return null;
  const rows:PublicationRow[]=[];
  for(const r of v.rows){if(!object(r)||!isUuid(r.id)||!count(r.position)||!count(r.quantity)||!str(r.status)||!rowStatuses.includes(r.status)||!["result_code","name","ean","sku","cost","price","minimum_price","commission","profit","problem"].every(k=>str(r[k]))||(r.weight_kg!==null&&!str(r.weight_kg)))return null;
    rows.push({product_info:readProductInfo(r.product_info),innpro_match:readInnproMatch(r.innpro_match),length_cm:str(r.length_cm)?r.length_cm:null,width_cm:str(r.width_cm)?r.width_cm:null,height_cm:str(r.height_cm)?r.height_cm:null,id:r.id,position:r.position,status:r.status,result_code:r.result_code as string,name:r.name as string,ean:r.ean as string,sku:r.sku as string,quantity:r.quantity,cost:r.cost as string,price:r.price as string,minimum_price:r.minimum_price as string,commission:r.commission as string,profit:r.profit as string,weight_kg:r.weight_kg as string|null,problem:r.problem as string});}
  return {version:v.version,id:v.id,seller_id:seller,account_id:v.account_id,marketplace:v.marketplace,account_name:v.account_name,view_name:v.view_name,status:v.status,total:v.total,filtered_total:v.filtered_total,counts,rules,created_at:v.created_at,rows};
}
export function readPublicationIndex(v:unknown,seller:string):PublicationIndex|null {
  const base=readWorkIndex(v,seller);if(!base||!object(v)||!Array.isArray(v.jobs)||v.jobs.length>30)return null;
  const jobs=v.jobs.map(j=>readPublicationJob(j,seller));if(jobs.some(j=>!j))return null;return {...base,jobs:jobs as PublicationJob[]};
}
export function readPublicationOptions(v:unknown,seller:string,account:string):Options|null {
  if(!object(v)||v.seller_id!==seller||v.account_id!==account)return null;const out:Options={};
  for(const k of ["shipping_groups","warehouses","logistic_classes","states"]){if(!(k in v))continue;const list=v[k];if(!Array.isArray(list)||list.length>100)return null;out[k]=[];for(const x of list){if(!object(x)||!str(x.id)||!str(x.name))return null;out[k].push({id:x.id,name:x.name});}}
  return out;
}
