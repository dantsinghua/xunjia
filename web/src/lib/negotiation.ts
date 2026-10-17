export interface V22Price { kind:'YIELD'|'NET_PRICE'|'DIRTY_PRICE'; value:string; settlement_date:string|null; computed_yield:string|null }
export interface V22Quote { side:'bid'|'ofr';price:V22Price|null;volume_wan:string|null;validity:string;finality:string;assertion:string;confirmed_day:string|null;revision:number }
export interface V22Peer {member_id:string;peer_uid:string;thread_id:string;send_allowed:number;analysis_pending:boolean;response_state:string;reconfirm_day:string|null;quotes:{bid:V22Quote;ofr:V22Quote};command_state:string|null}
export interface V22Group {group_id:string;bond_code:string;our_side:'BUY'|'SELL';target_volume:string;trade_day:string;send_allowed:number;state:string;review_needed:number;archived:number;plan:{name?:string};boundary:{kind:string;value:string}|null;summary:{reported_volume_wan:string;price_eligible_volume_wan:string;final_confirmed_volume_wan:string;goal_ready:boolean;allocation_is_manual:boolean};peers:V22Peer[]}
export interface V22State {engine:'v22';revision:number;send_allowed:boolean;groups:V22Group[];threads:unknown[]}
export function newerProjection(current:number,incoming:unknown):incoming is number {
  return typeof incoming==='number' && Number.isSafeInteger(incoming) && incoming>=0 && incoming>=current
}
