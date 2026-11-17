//! Durable background delivery. A database has exactly one worker lock owner.
mod mail;
mod processors;
pub use processors::execute_script;
use crate::{auth, catalog, common::*, fulfillment};
use fs2::FileExt;
use hmac::{Hmac, Mac};
use rusqlite::params;
use serde_json::{Value,json};
use sha2::Sha256;
use std::{net::ToSocketAddrs, os::unix::fs::OpenOptionsExt, sync::{Arc,atomic::{AtomicBool,Ordering}}, time::Duration};

pub fn transaction<T>(app:Arc<App>,work:impl FnOnce(&mut Context)->Result<T>)->Result<T> {
    let mut ctx=Context::background(app)?;ctx.conn.execute_batch("BEGIN IMMEDIATE")?;
    match work(&mut ctx){Ok(v)=>{ctx.conn.execute_batch("COMMIT")?;Ok(v)},Err(e)=>{let _=ctx.conn.execute_batch("ROLLBACK");Err(e)}}
}
fn event_authority(app:&App,entry:&Value)->Result<()> {
    let c=app.db()?;let event=required(&c,"SELECT e.payload,s.enabled FROM events e JOIN products p ON p.id=e.product_id JOIN shops s ON s.id=p.shop_id WHERE e.id=?",[text(entry,"id")],"通知已失效")?;
    if !flag(&event,"enabled"){return Err(ApiError::new(409,"店铺已停用"));}
    let payload=parse(text(&event,"payload"))?;
    if matches!(text(&payload,"type"),"redemption.requested"|"revision.requested") {
        let job=required(&c,"SELECT j.state,j.attempt,c.state AS card_state FROM jobs j JOIN cards c ON c.id=j.card_id WHERE j.id=? AND j.product_id=?",params![text(&payload["data"],"id"),text(&payload,"product_id")],"通知对应的任务已失效")?;
        if !matches!(text(&job,"state"),"queued"|"processing")||job["attempt"]!=payload["data"]["attempt"]||matches!(text(&job,"card_state"),"revoked"|"rejected"){return Err(ApiError::new(409,"通知对应的任务尝试已失效"));}
    }Ok(())
}
pub fn send_webhook(url:&str,secret:&str,payload:&str,event_id:&str,check:impl Fn()->Result<()>)->Result<()> {
    check()?;let u=url::Url::parse(url).map_err(|_|ApiError::bad("Webhook 地址无效"))?;
    if u.scheme()!="https"||u.host_str().is_none()||!u.username().is_empty()||u.password().is_some()||u.fragment().is_some(){return Err(ApiError::bad("Webhook 必须使用 HTTPS 公网地址"));}
    let host=u.host_str().unwrap();let addresses=(host,u.port_or_known_default().unwrap_or(443)).to_socket_addrs()?.collect::<Vec<_>>();
    if addresses.is_empty()||addresses.iter().any(|a|!catalog::public_ip(a.ip())){return Err(ApiError::bad("Webhook 地址必须解析到公网 IP"));}
    check()?;
    // Keep the URL hostname for Host and certificate verification; override only DNS.
    let client=reqwest::blocking::Client::builder().no_proxy().https_only(true).redirect(reqwest::redirect::Policy::none()).timeout(Duration::from_secs(15)).resolve(host,addresses[0]).build().map_err(|_|ApiError::new(502,"Webhook TLS 配置失败"))?;
    let timestamp=(now() as i64).to_string();let nonce=token();let mut mac=Hmac::<Sha256>::new_from_slice(secret.as_bytes()).map_err(|_|ApiError::bad("Webhook 密钥无效"))?;mac.update(timestamp.as_bytes());mac.update(b".");mac.update(nonce.as_bytes());mac.update(b".");mac.update(payload.as_bytes());let signature=mac.finalize().into_bytes().iter().map(|b|format!("{b:02x}")).collect::<String>();
    check()?;let response=client.post(u).header("Content-Type","application/json").header("X-Extore-Timestamp",timestamp).header("X-Extore-Nonce",nonce).header("X-Extore-Signature",signature).header("X-Extore-Event-Id",event_id).body(payload.to_owned()).send().map_err(|_|ApiError::new(502,"Webhook transport failed"))?;
    if !response.status().is_success(){return Err(ApiError::new(502,format!("HTTP {}",response.status().as_u16())));}Ok(())
}
pub fn deliver_webhook(app:&App,entry:&Value)->Result<()> {send_webhook(text(entry,"url"),text(entry,"secret"),text(entry,"payload"),text(entry,"id"),||event_authority(app,entry))}
fn outbox_once(app:Arc<App>)->Result<bool> {
    let selected=transaction(app.clone(),|ctx|{
        let Some(r)=row(&ctx.conn,"SELECT o.*,e.payload FROM outbox o JOIN events e ON e.id=o.id WHERE o.state='pending' AND o.due<=? ORDER BY o.due LIMIT 1",[now()])?else{return Ok(None)};
        let payload=parse(text(&r,"payload"))?;let mut allowed=row(&ctx.conn,"SELECT s.enabled FROM products p JOIN shops s ON s.id=p.shop_id WHERE p.id=?",[text(&payload,"product_id")])?.is_some_and(|s|flag(&s,"enabled"));
        if matches!(text(&payload,"type"),"redemption.requested"|"revision.requested") {let j=row(&ctx.conn,"SELECT j.*,EXISTS(SELECT 1 FROM card_task_flows f WHERE f.card_id=j.card_id) AS flow FROM jobs j WHERE j.id=?",[text(&payload["data"],"id")])?;allowed &= j.as_ref().is_some_and(|j|matches!(text(j,"state"),"queued"|"processing")&&j["attempt"]==payload["data"]["attempt"]&&!flag(j,"flow"));}
        if !allowed{ctx.conn.execute("UPDATE outbox SET state='cancelled',finished_at=? WHERE id=?",params![now(),text(&r,"id")])?;return Ok(Some((r,false)));}Ok(Some((r,true)))
    })?;
    let Some((r,allowed))=selected else{return Ok(false)};if !allowed{return Ok(true);}
    let result=deliver_webhook(&app,&r);let c=app.db()?;let attempts=integer(&r,"attempts",0)+1;
    if result.is_ok(){c.execute("UPDATE outbox SET state='delivered',attempts=attempts+1,error='',finished_at=? WHERE id=? AND state='pending'",params![now(),text(&r,"id")])?;}else{let dead=attempts>=8;let err=result.err().unwrap();c.execute("UPDATE outbox SET state=?,attempts=?,due=?,error=?,finished_at=? WHERE id=? AND state='pending'",params![if dead{"dead"}else{"pending"},attempts,now()+(5*2_i64.pow(attempts.min(20) as u32)).min(3600) as f64,err.message,if dead{Some(now())}else{None},text(&r,"id")])?;}Ok(true)
}
fn job_once(app:Arc<App>)->Result<bool> {
    let selected=transaction(app.clone(),|ctx|{
        for j in rows(&ctx.conn,"SELECT * FROM jobs WHERE state='processing' AND lease<? AND NOT EXISTS(SELECT 1 FROM card_task_flows f WHERE f.card_id=jobs.card_id)",[now()])?{fulfillment::apply_update(ctx,text(&j,"id"),&json!({"state":"failed","attempt":j["attempt"],"message":"处理超时或中断，待商家核实","retryable":false}))?;}
        for j in rows(&ctx.conn,"SELECT j.* FROM jobs j JOIN products p ON p.id=j.product_id JOIN shops s ON s.id=p.shop_id JOIN cards c ON c.id=j.card_id WHERE s.enabled=1 AND c.state NOT IN ('revoked','rejected') AND j.state='queued' AND json_extract(p.config,'$.mode') IN ('script','webhook') AND NOT EXISTS(SELECT 1 FROM card_task_flows f WHERE f.card_id=j.card_id) ORDER BY j.created,j.id LIMIT 100",[])? {
            let p=catalog::require_active(ctx,text(&j,"product_id"))?;fulfillment::apply_update(ctx,text(&j,"id"),&json!({"state":"processing","attempt":j["attempt"],"message":if text(&p,"mode")=="script"{"自动处理中"}else{"等待发货平台处理"}}))?;
            if text(&p,"mode")=="script"{ctx.conn.execute("UPDATE jobs SET lease=?,claimed_by='worker' WHERE id=?",params![now()+150.0,text(&j,"id")])?;return Ok(Some((j,p)));}
        }Ok(None)
    })?;
    let Some((job,product))=selected else{return Ok(false)};
    let result=execute_script(app.clone(),&job,&product,None).unwrap_or_else(|_|json!({"state":"failed","attempt":job["attempt"],"message":"处理器未正常完成，待商家核实是否已交付","retryable":false}));
    transaction(app,|ctx|{let j=required(&ctx.conn,"SELECT * FROM jobs WHERE id=?",[text(&job,"id")],"任务不存在")?;if text(&j,"state")=="processing"&&j["attempt"]==job["attempt"]{ctx.conn.execute_batch("SAVEPOINT processor_result")?;if fulfillment::apply_update(ctx,text(&j,"id"),&result).is_err(){ctx.conn.execute_batch("ROLLBACK TO processor_result")?;fulfillment::apply_update(ctx,text(&j,"id"),&json!({"state":"failed","attempt":j["attempt"],"message":"处理器返回的结果不符合商品定义，待商家核实","retryable":false}))?;}ctx.conn.execute_batch("RELEASE processor_result")?;}Ok(())})?;Ok(true)
}
type Pump=fn(Arc<App>)->Result<bool>;
async fn pump(app:Arc<App>,handler:Pump,stop:Arc<AtomicBool>) {
    while !stop.load(Ordering::Acquire){let state=tokio::task::spawn_blocking({let app=app.clone();move||handler(app)}).await;let busy=match state{Ok(Ok(v))=>v,Ok(Err(e))=>{tracing::warn!(status=e.status,"background operation failed");false},Err(_)=>{tracing::error!("background task panicked");false}};tokio::time::sleep(Duration::from_millis(if busy{100}else{1000})).await;}
}
pub async fn run(app:Arc<App>)->Result<()> {
    let lock=std::fs::OpenOptions::new().create(true).truncate(false).write(true).mode(0o600).open(app.data.join("worker.lock"))?;lock.try_lock_exclusive().map_err(|_|ApiError::new(409,"Only one worker per database is supported"))?;
    let stop=Arc::new(AtomicBool::new(false));let mut pumps=tokio::task::JoinSet::new();
    for handler in [job_once as Pump,outbox_once,mail::once,fulfillment::worker_once] {pumps.spawn(pump(app.clone(),handler,stop.clone()));}
    tracing::info!("Extore Rust worker running");crate::shutdown().await;stop.store(true,Ordering::Release);while pumps.join_next().await.is_some(){};FileExt::unlock(&lock)?;Ok(())
}
