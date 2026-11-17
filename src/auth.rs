use crate::common::*;
use argon2::{Argon2,PasswordHash,PasswordHasher,PasswordVerifier,password_hash::SaltString};
use rusqlite::params;
use serde_json::{Value,json};
use std::collections::HashSet;
pub mod secret;
mod passkeys;
pub mod account;
pub mod cli;

pub const PERMISSIONS:&[&str]=&["queue.view","queue.process","queue.retry","product.edit","fulfillment.configure","cards.manage","events.manage","links.delegate","product.delete","product.purge","queue.monitor"];
fn denied()->ApiError{ApiError::new(401,"请先登录")}
pub fn shop(ctx:&Context,sid:&str)->Result<Value>{row(&ctx.conn,"SELECT * FROM shops WHERE id=? AND enabled=1",[sid])?.ok_or_else(||ApiError::new(401,"店铺不可用"))}
pub fn require_recent(s:&Value)->Result<()>{recent(s)}
pub fn recent(s:&Value)->Result<()>{if text(s,"channel")!="cli"&&now()-number(s,"auth_at")>600.0{Err(ApiError::new(401,"请重新认证后再修改安全设置"))}else{Ok(())}}
pub fn permission_list(value:&Value)->Result<Vec<String>>{
 let parsed=if let Some(s)=value.as_str(){parse(s)?}else{value.clone()};
 let values=parsed.as_array().ok_or_else(denied)?;let mut out=Vec::new();
 for v in values{let p=v.as_str().filter(|p|PERMISSIONS.contains(p)).ok_or_else(denied)?;if !out.iter().any(|x|x==p){out.push(p.to_owned());}}
 if out.is_empty()||(out.iter().any(|x|x=="queue.process"||x=="queue.retry")&&!out.iter().any(|x|x=="queue.view"))||(out.iter().any(|x|x=="fulfillment.configure")&&!out.iter().any(|x|x=="product.edit")){return Err(denied());}
 Ok(out)
}
fn validate_pipeline(ctx:&Context,staff:&Value)->Result<Option<Value>>{
 let Some(binding)=row(&ctx.conn,"SELECT * FROM pipeline_bindings WHERE staff_id=?",[text(staff,"id")])? else{return Ok(None)};
 let invalid=||ApiError::new(401,"商品流水线授权已过期或撤销");
 let scope=row(&ctx.conn,"SELECT * FROM pipeline_authorizations WHERE id=? AND revoked=0 AND expires>?",params![text(&binding,"authorization_id"),now()])?.ok_or_else(invalid)?;
 if !["product","shop.pipeline"].contains(&text(&scope,"kind")){return Err(invalid());}
 shop(ctx,text(&scope,"shop_id"))?;
 let granted=permission_list(&scope["permissions"])?;let current=permission_list(&staff["permissions"])?;
 let device=row(&ctx.conn,"SELECT * FROM cli_devices WHERE id=? AND revoked=0",[text(&binding,"device_id")])?.ok_or_else(invalid)?;
 if binding["product_id"]!=staff["product_id"]||!staff["parent_id"].is_null()||number(staff,"expires")>number(&scope,"expires")||current.iter().any(|p|!granted.contains(p))||device["staff_id"]!=staff["id"]||device["public_key"]!=scope["public_key"]||device["fingerprint"]!=scope["fingerprint"]{return Err(invalid());}
 let product=row(&ctx.conn,"SELECT shop_id,config FROM products WHERE id=?",[text(staff,"product_id")])?.ok_or_else(invalid)?;
 if product["shop_id"]!=scope["shop_id"]{return Err(invalid());}
 if text(&scope,"kind")=="shop.pipeline"&&parse(text(&product,"config"))?.get("mode").and_then(Value::as_str).unwrap_or("manual")!="manual"{return Err(invalid());}
 Ok(Some(scope))
}
pub fn staff_authorization(ctx:&Context,staff_id:&str)->Result<Value>{
 let mut cursor=staff_id.to_owned();let mut seen=HashSet::new();let mut result:Option<Value>=None;let mut effective=Vec::new();let mut expiry=f64::INFINITY;
 while !cursor.is_empty(){
  if !seen.insert(cursor.clone()){return Err(denied());}
  let value=row(&ctx.conn,"SELECT * FROM staff WHERE id=? AND revoked=0 AND archived=0 AND expires>?",params![cursor,now()])?.ok_or_else(denied)?;
  let permissions=permission_list(&value["permissions"])?;validate_pipeline(ctx,&value)?;
  if let Some(original)=&result{if original["product_id"]!=value["product_id"]{return Err(denied());}effective.retain(|p|permissions.contains(p));}else{effective=permissions;result=Some(value.clone());}
  if effective.is_empty(){return Err(denied());}
  expiry=expiry.min(number(&value,"expires"));cursor=text(&value,"parent_id").to_owned();
 }
 let mut result=result.ok_or_else(denied)?;
 let product=row(&ctx.conn,"SELECT shop_id FROM products WHERE id=?",[text(&result,"product_id")])?.ok_or_else(denied)?;
 let sid=product["shop_id"].as_str().map(String::from).unwrap_or(setting(&ctx.conn,"legacy_shop_id","")?);
 shop(ctx,&sid)?;result["shop_id"]=json!(sid);result["permissions"]=json!(effective);result["expires"]=json!(expiry);Ok(result)
}
fn cli_route(ctx:&Context,role:&str)->bool{
 let path=ctx.path.as_str();let method=ctx.method.as_str();
 if role=="staff"{return path.starts_with("/api/manage/")||matches!((method,path),("GET","/api/cli/status")|("DELETE","/api/cli/session"));}
 if role!="admin"{return false;}
 path.starts_with("/api/admin/")||path.starts_with("/api/manage/")||path.starts_with("/api/platform/")||path.starts_with("/api/shop/")||matches!((method,path),("GET","/api/cli/owner/status")|("DELETE","/api/cli/owner/session")|("POST","/api/cli/owner/action-challenge")|("GET","/api/auth/status")|("GET","/api/auth/passkeys")|("POST","/api/auth/register/options")|("POST","/api/auth/register/verify")|("POST","/api/auth/reauth/password")|("POST","/api/auth/password/change")|("POST","/api/auth/totp/setup")|("POST","/api/auth/totp/confirm")|("POST","/api/auth/totp/disable")|("POST","/api/auth/totp/backup-codes"))||(method=="DELETE"&&path.starts_with("/api/auth/passkeys/"))
}
pub fn session(ctx:&Context,roles:&[&str],permission:Option<&str>)->Result<Value>{
 let cookie=ctx.cookie("extore_session");
 let (key,channel)=if ctx.headers.contains_key("authorization"){
  if !cookie.is_empty(){return Err(ApiError::new(401,"不能混用浏览器与 CLI 登录凭证"));}
  let mut parts=ctx.header("authorization").split(' ');let kind=parts.next().unwrap_or("");let token=parts.next().unwrap_or("");
  if !kind.eq_ignore_ascii_case("bearer")||token.is_empty()||parts.next().is_some(){return Err(denied());}(digest(token),"cli")
 }else{(digest(cookie),"browser")};
 let mut s=row(&ctx.conn,"SELECT * FROM sessions WHERE digest=? AND expires>? AND revoked=0 AND channel=?",params![key,now(),channel])?.ok_or_else(denied)?;
 if !roles.contains(&text(&s,"role")){return Err(denied());}
 if channel=="cli"&&!cli_route(ctx,text(&s,"role")){return Err(ApiError::new(401,"此接口不接受该 CLI 登录凭证"));}
 if now()-number(&s,"last_seen")>=30.0{ctx.conn.execute("UPDATE sessions SET last_seen=? WHERE digest=?",params![now(),key])?;}
 if channel=="cli"{
  let device=if text(&s,"role")=="admin"{
   if !s["staff_id"].is_null()||!s["device_id"].is_null()||text(&s,"owner_device_id").is_empty(){return Err(denied());}
   cli::owner_device(ctx,text(&s,"owner_device_id"))?
  }else{
   if !s["owner_device_id"].is_null(){return Err(denied());}
   row(&ctx.conn,"SELECT * FROM cli_devices WHERE id=? AND staff_id=? AND revoked=0",params![text(&s,"device_id"),text(&s,"staff_id")])?.ok_or_else(denied)?
  };
  if text(&s,"role")=="admin"{if device["shop_id"]!=s["shop_id"]{return Err(denied());}s["scope"]=json!("shop.owner");s["grant_expires"]=device["expires"].clone();s["fingerprint"]=device["fingerprint"].clone();}
  s["client_name"]=device["client_name"].clone();s["agent_type"]=device["agent_type"].clone();
  let table=if text(&s,"role")=="admin"{"owner_cli_devices"}else{"cli_devices"};
  if now()-number(&device,"last_seen")>=30.0{ctx.conn.execute(&format!("UPDATE {table} SET last_seen=? WHERE id=?"),params![now(),text(&device,"id")])?;}
 }
 if text(&s,"role")=="admin"{
  if let Some(sid)=s["shop_id"].as_str(){shop(ctx,sid)?;}
  s["account_id"]=json!(actor(&s));s["permissions"]=json!(PERMISSIONS);
 }else if text(&s,"role")=="staff"{
  let staff=staff_authorization(ctx,text(&s,"staff_id"))?;
  if !s["shop_id"].is_null()&&s["shop_id"]!=staff["shop_id"]{return Err(denied());}
  if channel=="cli"{
   let bindings=rows(&ctx.conn,"SELECT * FROM pipeline_bindings WHERE device_id=? OR staff_id=?",params![text(&s,"device_id"),text(&s,"staff_id")])?;
   if !bindings.is_empty(){
    if bindings.len()!=1||bindings[0]["device_id"]!=s["device_id"]||bindings[0]["staff_id"]!=s["staff_id"]{return Err(denied());}
    let scope=validate_pipeline(ctx,&staff)?.ok_or_else(denied)?;
    s["authorization_id"]=scope["id"].clone();s["authorization_revision"]=scope["revision"].clone();s["scope"]=scope["kind"].clone();
   }
  }
  for key in ["product_id","shop_id","permissions","name","parent_id","max_uses","uses","max_cli_uses","cli_uses"]{s[key]=staff[key].clone();}
  s["link_expires"]=staff["expires"].clone();
  s["remaining_uses"]=json!((integer(&staff,"max_uses",0)-integer(&staff,"uses",0)).max(0));
  s["remaining_cli_uses"]=json!((integer(&staff,"max_cli_uses",0)-integer(&staff,"cli_uses",0)).max(0));
 }
 if let Some(p)=permission{if !s["permissions"].as_array().is_some_and(|a|a.iter().any(|v|v==p)){return Err(ApiError::new(403,"此商品管理链接没有执行该操作的权限"));}}
 let actual=s["shop_id"].as_str().unwrap_or("platform");
 if (ctx.headers.contains_key("x-extore-shop-scope")&&ctx.header("x-extore-shop-scope")!=actual)||(ctx.headers.contains_key("x-extore-session-id")&&ctx.header("x-extore-session-id")!=text(&s,"id")){return Err(ApiError::new(409,"登录账号已切换，请刷新页面后重试"));}
 Ok(s)
}
pub fn selected_shop(ctx:&Context,s:&Value,requested:&str)->Result<Option<String>>{
 if let Some(own)=s["shop_id"].as_str(){if !requested.is_empty()&&requested!=own{return Err(ApiError::new(403,"没有此店铺的管理权限"));}shop(ctx,own)?;return Ok(Some(own.to_owned()));}
 if text(s,"role")!="admin"{return Err(denied());}
 if requested.is_empty(){Ok(None)}else{shop(ctx,requested)?;Ok(Some(requested.to_owned()))}
}
pub fn authorize_product(ctx:&Context,s:&Value,pid:&str)->Result<Value>{
 let p=required(&ctx.conn,"SELECT * FROM products WHERE id=?",[pid],"商品不存在")?;let sid=text(&p,"shop_id");
 if (text(s,"role")=="staff"&&text(s,"product_id")!=pid)||(text(s,"role")=="admin"&&!s["shop_id"].is_null()&&text(s,"shop_id")!=sid){return Err(ApiError::new(403,"没有此商品的管理权限"));}
 if !["admin","staff"].contains(&text(s,"role")){return Err(denied());}shop(ctx,sid)?;Ok(p)
}
pub fn revoke_session(ctx:&Context,key:&str,who:&str,action:&str)->Result<()>{
 let Some(s)=row(&ctx.conn,"SELECT * FROM sessions WHERE digest=? AND revoked=0",[key])? else{return Ok(())};
 ctx.conn.execute("UPDATE sessions SET revoked=1 WHERE digest=?",[key])?;audit(&ctx.conn,who,action,text(&s,"id"),s["shop_id"].as_str())?;
 if ["admin","staff"].contains(&text(&s,"role")){
  let remaining=if text(&s,"role")=="staff"{row(&ctx.conn,"SELECT 1 FROM sessions WHERE staff_id=? AND revoked=0 AND expires>? LIMIT 1",params![text(&s,"staff_id"),now()])?}else{row(&ctx.conn,"SELECT 1 FROM sessions WHERE role='admin' AND shop_id IS ? AND revoked=0 AND expires>? LIMIT 1",params![s["shop_id"].as_str(),now()])?};
  if remaining.is_none(){ctx.conn.execute("UPDATE jobs SET state='queued',claimed_by=NULL,lease=NULL,updated=? WHERE claimed_by=? AND state='processing' AND product_id IN (SELECT id FROM products WHERE COALESCE(json_extract(config,'$.mode'),'manual')='manual')",params![now(),actor(&s)])?;}
 }
 Ok(())
}
pub fn create_session(ctx:&mut Context,role:&str,sid:Option<&str>,staff:Option<&str>,method:&str)->Result<String>{
 let previous=digest(ctx.cookie("extore_session"));let who=staff.map(String::from).unwrap_or_else(||sid.map(|v|format!("shop:{v}")).unwrap_or_else(||"owner".into()));
 revoke_session(ctx,&previous,&who,"session.replace")?;
 let value=token();let key=digest(&value);let session_id=id();let max_age=if role=="bootstrap"{600}else{28800};
 let ua=ctx.header("user-agent").chars().map(|c|if c.is_control(){' '}else{c}).take(300).collect::<String>();
 ctx.conn.execute("INSERT INTO sessions(digest,role,staff_id,expires,created,id,last_seen,ip,ua,revoked,shop_id,auth_at,auth_method) VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?)",params![key,role,staff,now()+max_age as f64,now(),session_id,now(),ctx.peer,ua,sid,now(),method])?;
 audit(&ctx.conn,&who,"session.create",&session_id,sid)?;
 ctx.set_cookie(&format!("extore_session={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{}",if ctx.app.origin.starts_with("https:"){"; Secure"}else{""}))?;Ok(value)
}
pub fn throttle(ctx:&Context,bucket:&str,limit:i64,window:f64,account:Option<&str>)->Result<()>{
 let mut keys=vec![format!("{bucket}:{}",ctx.peer)];if let Some(a)=account{keys.push(format!("{bucket}-account:{}",digest(a)));}
 ctx.conn.execute("DELETE FROM rate_limits WHERE expires<?",[now()])?;
 for key in keys{
  if row(&ctx.conn,"SELECT count FROM rate_limits WHERE key=?",[&key])?.is_some_and(|v|integer(&v,"count",0)>=limit){return Err(ApiError::new(429,"请求过于频繁，请稍后重试"));}
  ctx.conn.execute("INSERT INTO rate_limits VALUES (?,1,?) ON CONFLICT(key) DO UPDATE SET count=count+1",params![key,now()+window])?;
 }
 ctx.conn.execute_batch("COMMIT; BEGIN IMMEDIATE")?;Ok(())
}
pub fn password_valid(hash:&str,password:&str)->bool{PasswordHash::new(hash).ok().is_some_and(|h|Argon2::default().verify_password(password.as_bytes(),&h).is_ok())}
pub fn password_hash(password:&str)->Result<String>{
 let salt=SaltString::generate(&mut rand::rngs::OsRng);
 Argon2::new(argon2::Algorithm::Argon2id,argon2::Version::V0x13,argon2::Params::new(65536,3,4,None).map_err(|_|ApiError::new(500,"密码加密参数错误"))?).hash_password(password.as_bytes(),&salt).map(|v|v.to_string()).map_err(|_|ApiError::new(500,"密码加密失败"))
}
pub fn revoke_shop_auth(ctx:&Context,sid:Option<&str>)->Result<()>{
 for s in rows(&ctx.conn,"SELECT digest FROM sessions WHERE shop_id IS ? AND role IN ('admin','bootstrap') AND revoked=0",[sid])?{revoke_session(ctx,text(&s,"digest"),"owner","session.auth_reset")?;}
 ctx.conn.execute("UPDATE owner_cli_devices SET revoked=1 WHERE shop_id IS ?",[sid])?;
 ctx.conn.execute("UPDATE cli_devices SET revoked=1 WHERE staff_id IN (SELECT s.id FROM staff s JOIN products p ON p.id=s.product_id WHERE p.shop_id IS ?)",[sid])?;
 ctx.conn.execute("UPDATE sessions SET revoked=1 WHERE staff_id IN (SELECT s.id FROM staff s JOIN products p ON p.id=s.product_id WHERE p.shop_id IS ?)",[sid])?;
 if let Some(sid)=sid{ctx.conn.execute("UPDATE pipeline_authorizations SET revoked=1 WHERE shop_id=?",[sid])?;}else{ctx.conn.execute("UPDATE pipeline_authorizations SET revoked=1 WHERE issuer_role='root'",[])?;}
 ctx.conn.execute("DELETE FROM challenges WHERE session_digest IN (SELECT digest FROM sessions WHERE shop_id IS ? AND role IN ('admin','bootstrap'))",[sid])?;
 ctx.conn.execute("UPDATE commerce_grants SET revoked=1 WHERE shop_id IS ? OR (? IS NULL AND issuer_role='root')",params![sid,sid])?;
 cli::invalidate_pending_owner_approvals(ctx,sid)?;
 if let Some(sid)=sid{ctx.conn.execute("DELETE FROM account_tokens WHERE shop_id=? AND used IS NULL",[sid])?;}Ok(())
}
pub fn reset_auth(app:&App)->Result<()>{
 let c=app.db()?;let raw=token();let hashed=password_hash(&raw)?;c.execute_batch("BEGIN IMMEDIATE")?;
 c.execute("DELETE FROM credentials WHERE shop_id IS NULL",[])?;c.execute("UPDATE sessions SET revoked=1 WHERE shop_id IS NULL AND role IN ('admin','bootstrap')",[])?;
 c.execute("UPDATE owner_cli_devices SET revoked=1 WHERE shop_id IS NULL",[])?;c.execute("UPDATE pipeline_authorizations SET revoked=1 WHERE issuer_role='root'",[])?;
 set_setting(&c,"bootstrap_password",&hashed)?;c.execute_batch("COMMIT")?;
 let path=app.data.join("bootstrap-password.txt");std::fs::write(&path,format!("{raw}\n"))?;
 #[cfg(unix)]{use std::os::unix::fs::PermissionsExt;std::fs::set_permissions(path,std::fs::Permissions::from_mode(0o600))?;}Ok(())
}
pub fn handle(ctx:&mut Context)->Result<Option<Reply>>{
 if let Some(r)=passkeys::handle(ctx)?{return Ok(Some(r));}
 if let Some(r)=account::handle(ctx)?{return Ok(Some(r));}
 if let Some(r)=cli::handle(ctx)?{return Ok(Some(r));}
 if ctx.is("GET","/api/auth/status"){
  let configured=!setting(&ctx.conn,"bootstrap_password","")?.is_empty();
  let count=row(&ctx.conn,"SELECT count(*) n FROM credentials WHERE shop_id IS NULL",[])?.map(|v|integer(&v,"n",0)).unwrap_or(0);
  let s=match session(ctx,&["admin","bootstrap","staff"],None){Ok(v)=>Some(v),Err(e)if e.status<500=>None,Err(e)=>return Err(e)};
  let mut result=json!({"actor":null,"configured":configured||count>0,"password_enabled":configured&&count==0,"role":null,"registration_enabled":setting(&ctx.conn,"registration_enabled","false")?=="true","session_id":null,"shop_id":null,"superadmin":false});
  if let Some(s)=s{
   for k in ["role","shop_id","channel","scope","expires","grant_expires","client_name","fingerprint"]{if !s[k].is_null(){result[k]=s[k].clone();}}
   result["session_id"]=s["id"].clone();result["superadmin"]=json!(text(&s,"role")=="admin"&&s["shop_id"].is_null());
   result["actor"]=json!(actor(&s));
   if text(&s,"channel")=="cli"{result["device_id"]=if text(&s,"role")=="admin"{s["owner_device_id"].clone()}else{s["device_id"].clone()};result["agent_type"]=s["agent_type"].clone();}
   if let Some(sid)=s["shop_id"].as_str(){if text(&s,"role")=="admin"{let own=shop(ctx,sid)?;result["shop_name"]=own["name"].clone();result["shop_email"]=own["email"].clone();}}
   if text(&s,"role")=="staff"{for k in ["product_id","permissions","parent_id","max_uses","uses","remaining_uses","max_cli_uses","cli_uses","remaining_cli_uses"]{result[k]=s[k].clone();}result["link_id"]=s["staff_id"].clone();result["link_name"]=s["name"].clone();result["link_expires"]=s["link_expires"].clone();}
  }
  return Ok(Some(Reply::json(result)));
 }
 if ctx.is("POST","/api/auth/password"){
  throttle(ctx,"login",5,300.0,None)?;
  if row(&ctx.conn,"SELECT 1 FROM credentials WHERE shop_id IS NULL LIMIT 1",[])?.is_some(){return Err(ApiError::new(403,"密码登录已禁用，请使用 Passkey"));}
  if !password_valid(&setting(&ctx.conn,"bootstrap_password","")?,ctx.b("password")){return Err(ApiError::new(401,"密码错误"));}
  create_session(ctx,"bootstrap",None,None,"bootstrap")?;return Ok(Some(Reply::json(json!({"role":"bootstrap"}))));
 }
 if ctx.is("POST","/api/auth/logout"){
  revoke_session(ctx,&digest(ctx.cookie("extore_session")),"owner","session.logout")?;ctx.set_cookie("extore_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")?;
  return Ok(Some(Reply::json(json!({"ok":true}))));
 }
 if ctx.is("POST","/api/staff/login"){
  throttle(ctx,"staff-login",10,60.0,None)?;
  let value=if !ctx.b("token").is_empty(){ctx.b("token")}else{ctx.b("key")};
  let staff=required(&ctx.conn,"SELECT * FROM staff WHERE digest=?",[digest(value)],"商品管理链接无效")?;let auth=staff_authorization(ctx,text(&staff,"id"))?;
  if let Ok(current)=session(ctx,&["staff"],None){if current["staff_id"]==auth["id"]{return Ok(Some(Reply::json(json!({"role":"staff","product_id":auth["product_id"],"permissions":auth["permissions"]}))));}}
  if integer(&auth,"uses",0)>=integer(&auth,"max_uses",0){return Err(ApiError::new(409,"管理链接浏览器使用次数已用完"));}
  ctx.conn.execute("UPDATE staff SET uses=uses+1 WHERE id=?",[text(&auth,"id")])?;
  create_session(ctx,"staff",auth["shop_id"].as_str(),auth["id"].as_str(),"management_link")?;
  return Ok(Some(Reply::json(json!({"role":"staff","product_id":auth["product_id"],"permissions":auth["permissions"]}))));
 }Ok(None)
}
