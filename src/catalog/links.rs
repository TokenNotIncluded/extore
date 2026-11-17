use crate::{auth, common::*};
use rusqlite::params;
use serde_json::{Value,json};
use std::collections::BTreeSet;

const PERMISSIONS:[&str;11]=["queue.view","queue.process","queue.retry","product.edit","fulfillment.configure","cards.manage","events.manage","links.delegate","product.delete","product.purge","queue.monitor"];
fn view(mut value:Value)->Value {
    value.as_object_mut().unwrap().remove("digest");value["permissions"]=json_field(&value,"permissions",json!([]));
    value["remaining_uses"]=json!((integer(&value,"max_uses",1)-integer(&value,"uses",0)).max(0));value["remaining_cli_uses"]=json!((integer(&value,"max_cli_uses",1)-integer(&value,"cli_uses",0)).max(0));value
}
pub fn create(ctx:&Context,s:&Value,body:&Value)->Result<Value> {
    let pid=text(body,"product_id");auth::authorize_product(ctx,s,pid)?;let p=super::require_active(ctx,pid)?;
    let name=text(body,"name").trim();if name.is_empty()||name.chars().count()>100{return Err(ApiError::bad("请填写管理链接名称"));}
    let permissions=body.get("permissions").cloned().unwrap_or(json!(["queue.view","queue.process"]));
    let requested=permissions.as_array().ok_or_else(||ApiError::bad("权限必须是列表"))?.iter().map(|v|v.as_str().unwrap_or("").to_owned()).collect::<BTreeSet<_>>();
    if requested.is_empty()||requested.iter().any(|p|!PERMISSIONS.contains(&p.as_str()))||((requested.contains("queue.process")||requested.contains("queue.retry"))&&!requested.contains("queue.view"))||(requested.contains("fulfillment.configure")&&!requested.contains("product.edit")){return Err(ApiError::bad("商品权限组合无效"));}
    let max=integer(body,"max_uses",1);let max_cli=integer(body,"max_cli_uses",1);let days=body.get("days").and_then(Value::as_f64).unwrap_or(7.0);
    if !(1..=1000).contains(&max)||!(1..=1000).contains(&max_cli)||days<=0.0||days>90.0{return Err(ApiError::bad("链接次数或有效期无效"));}
    let mut expires=now()+days*86400.0;let parent=if text(s,"role")=="staff"{Some(text(s,"staff_id"))}else{None};
    if let Some(parent)=parent {
        let old=required(&ctx.conn,"SELECT * FROM staff WHERE id=?",[parent],"上级链接不存在")?;
        let allowed=s["permissions"].as_array().ok_or_else(||ApiError::new(403,"没有委派权限"))?.iter().map(|v|v.as_str().unwrap_or("").to_owned()).collect::<BTreeSet<_>>();
        if !allowed.contains("links.delegate")||!requested.is_subset(&allowed)||requested==allowed||max>integer(&old,"max_uses",1)||max_cli>integer(&old,"max_cli_uses",1){return Err(ApiError::new(403,"下级授权必须缩小权限与可用次数"));}
        if body.get("days").is_none(){expires=expires.min(number(s,"link_expires"));}else if expires>number(s,"link_expires"){return Err(ApiError::new(403,"下级链接不能超过上级有效期"));}
    }
    let sid=id();let secret=token();let permissions=PERMISSIONS.iter().filter(|p|requested.contains(**p)).copied().collect::<Vec<_>>();
    ctx.conn.execute("INSERT INTO staff(id,digest,product_id,name,expires,permissions,parent_id,created,max_uses,max_cli_uses) VALUES (?,?,?,?,?,?,?,?,?,?)",params![sid,digest(&secret),pid,name,expires,json!(permissions).to_string(),parent,now(),max,max_cli])?;
    audit(&ctx.conn,&actor(s),"staff.create",&sid,Some(text(&p,"shop_id")))?;let mut result=view(required(&ctx.conn,"SELECT * FROM staff WHERE id=?",[&sid],"授权不存在")?);result["url"]=json!(format!("{}/staff#{}",ctx.app.origin,secret));Ok(result)
}
pub fn handle(ctx:&mut Context)->Result<Option<Reply>> {
    let admin=ctx.path.starts_with("/api/admin/staff");let managed=ctx.path.starts_with("/api/manage/links");if !admin&&!managed{return Ok(None);}
    let s=auth::session(ctx,if admin{&["admin"]}else{&["admin","staff"]},Some("links.delegate"))?;
    if ctx.method==axum::http::Method::POST&&(ctx.path.ends_with("/staff")||ctx.path.ends_with("/links")){return Ok(Some(Reply::json(create(ctx,&s,&ctx.body)?)));}
    if ctx.method==axum::http::Method::GET {
        let pid=if text(&s,"role")=="staff"{text(&s,"product_id")}else{ctx.q("product_id")};if !pid.is_empty(){auth::authorize_product(ctx,&s,pid)?;}
        let own=auth::selected_shop(ctx,&s,ctx.q("shop_id"))?;let values=rows(&ctx.conn,"SELECT s.* FROM staff s JOIN products p ON p.id=s.product_id WHERE (?='' OR s.product_id=?) AND (? IS NULL OR p.shop_id=?) AND NOT EXISTS(SELECT 1 FROM pipeline_bindings b WHERE b.staff_id=s.id) ORDER BY s.created DESC LIMIT 500",params![pid,pid,own,own])?.into_iter().map(view).collect::<Vec<_>>();return Ok(Some(Reply::json(json!(values))));
    }
    if ctx.path.ends_with("/revoke")&&ctx.method==axum::http::Method::POST {
        let sid=ctx.path.split('/').nth(4).unwrap_or("");let link=required(&ctx.conn,"SELECT * FROM staff WHERE id=?",[sid],"管理链接不存在")?;auth::authorize_product(ctx,&s,text(&link,"product_id"))?;
        if text(&s,"role")=="staff" {let mut at=text(&link,"parent_id").to_owned();let mut allowed=false;for _ in 0..100 {if at==text(&s,"staff_id"){allowed=true;break;}if at.is_empty(){break;}at=row(&ctx.conn,"SELECT parent_id FROM staff WHERE id=?",[&at])?.map(|v|text(&v,"parent_id").to_owned()).unwrap_or_default();}if !allowed{return Err(ApiError::new(403,"只能撤销下级链接"));}}
        ctx.conn.execute("WITH RECURSIVE children(id) AS (SELECT id FROM staff WHERE id=? UNION ALL SELECT s.id FROM staff s JOIN children c ON s.parent_id=c.id) UPDATE staff SET revoked=1 WHERE id IN (SELECT id FROM children)",[sid])?;
        ctx.conn.execute("UPDATE sessions SET revoked=1 WHERE staff_id IN (SELECT id FROM staff WHERE revoked=1)",[])?;
        audit(&ctx.conn,&actor(&s),"staff.revoke",sid,None)?;return Ok(Some(Reply::json(json!({"ok":true}))));
    }
    Ok(None)
}
