use super::*;
use base64::{Engine,engine::general_purpose::URL_SAFE_NO_PAD};
use serde_cbor_2::Value as Cbor;
use std::{collections::BTreeMap,time::Duration};
use webauthn_rs_core::{WebauthnCore,proto::*};

fn invalid()->ApiError{ApiError::new(401,"Passkey 校验失败，请重试")}
fn webauthn(ctx:&Context)->Result<WebauthnCore>{
 let origin=url::Url::parse(&ctx.app.origin).map_err(|_|ApiError::new(500,"站点地址无效"))?;
 let rp=origin.host_str().ok_or_else(||ApiError::new(500,"站点地址无效"))?.to_owned();
 Ok(WebauthnCore::new_unsafe_experts_only("Extore · 兑所",&rp,vec![origin],Duration::from_secs(300),Some(false),Some(false)))
}
fn credential(ctx:&Context,value:&Value)->Result<Credential>{
 let bytes=URL_SAFE_NO_PAD.decode(text(value,"public_key")).map_err(|_|invalid())?;
 let cbor:Cbor=serde_cbor_2::from_slice(&bytes).map_err(|_|invalid())?;
 let key=COSEKey::try_from(&cbor).map_err(|_|invalid())?;
 let metadata=parse(&setting(&ctx.conn,&format!("passkey-metadata:{}",text(value,"id")),"{}")?)?;
 Ok(Credential{
  cred_id:URL_SAFE_NO_PAD.decode(text(value,"id")).map_err(|_|invalid())?.into(),cred:key,
  counter:integer(value,"sign_count",0).try_into().map_err(|_|invalid())?,transports:None,user_verified:true,
  backup_eligible:flag(&metadata,"backup_eligible"),backup_state:flag(&metadata,"backup_state"),
  registration_policy:UserVerificationPolicy::Required,extensions:RegisteredExtensions::none(),attestation:ParsedAttestation::default(),attestation_format:AttestationFormat::None,
 })
}
fn cose_bytes(key:&COSEKey)->Result<Vec<u8>>{
 let mut map=BTreeMap::new();map.insert(Cbor::Integer(3),Cbor::Integer(key.type_ as i128));
 match &key.key{
  COSEKeyType::EC_EC2(k)=>{
   map.insert(Cbor::Integer(1),Cbor::Integer(2));map.insert(Cbor::Integer(-1),Cbor::Integer(k.curve.clone() as i128));
   map.insert(Cbor::Integer(-2),Cbor::Bytes(k.x.as_slice().to_vec()));map.insert(Cbor::Integer(-3),Cbor::Bytes(k.y.as_slice().to_vec()));
  }
  COSEKeyType::EC_OKP(k)=>{
   map.insert(Cbor::Integer(1),Cbor::Integer(1));map.insert(Cbor::Integer(-1),Cbor::Integer(k.curve.clone() as i128));map.insert(Cbor::Integer(-2),Cbor::Bytes(k.x.as_slice().to_vec()));
  }
  COSEKeyType::RSA(k)=>{
   map.insert(Cbor::Integer(1),Cbor::Integer(3));map.insert(Cbor::Integer(-1),Cbor::Bytes(k.n.as_slice().to_vec()));map.insert(Cbor::Integer(-2),Cbor::Bytes(k.e.to_vec()));
  }
 }
 serde_cbor_2::to_vec(&Cbor::Map(map)).map_err(|_|ApiError::new(500,"Passkey 保存失败"))
}
fn save_challenge(ctx:&mut Context,kind:&str,s:Option<&Value>,state:&impl serde::Serialize,mut options:Value)->Result<Value>{
 let key=token();let state=serde_json::to_vec(state)?;
 ctx.conn.execute("DELETE FROM challenges WHERE expires<?",[now()])?;
 ctx.conn.execute("INSERT INTO challenges(digest,challenge,kind,session_digest,expires) VALUES (?,?,?,?,?)",params![digest(&key),state,kind,s.and_then(|s|s["digest"].as_str()),now()+300.0])?;
 if s.is_some_and(|s|text(s,"channel")=="cli"){options["challenge_id"]=json!(key);}else{ctx.set_cookie(&format!("extore_challenge={key}; Path=/api/auth; HttpOnly; SameSite=Strict; Max-Age=300{}",if ctx.app.origin.starts_with("https:"){"; Secure"}else{""}))?;}
 Ok(options)
}
fn consume<T:serde::de::DeserializeOwned>(ctx:&Context,kind:&str,s:Option<&Value>)->Result<T>{
 let value=if s.is_some_and(|s|text(s,"channel")=="cli"){ctx.b("challenge_id")}else{ctx.cookie("extore_challenge")};
 let ch=row(&ctx.conn,"SELECT * FROM challenges WHERE digest=? AND kind=? AND expires>?",params![digest(value),kind,now()])?.ok_or_else(||ApiError::bad("认证请求已过期，请重新开始"))?;
 if s.is_some_and(|s|s["digest"]!=ch["session_digest"]){return Err(invalid());}
 ctx.conn.execute("DELETE FROM challenges WHERE digest=?",[text(&ch,"digest")])?;
 // Failed assertions also consume the challenge.
 ctx.conn.execute_batch("COMMIT; BEGIN IMMEDIATE")?;
 let bytes=URL_SAFE_NO_PAD.decode(text(&ch,"challenge")).map_err(|_|invalid())?;
 serde_json::from_slice(&bytes).map_err(|_|ApiError::bad("认证请求已过期，请重新开始"))
}
pub fn handle(ctx:&mut Context)->Result<Option<Reply>>{
 let path=ctx.path.clone();
 if ctx.method.as_str()=="POST"&&matches!(path.as_str(),"/api/auth/register/options"|"/api/auth/login/options"|"/api/auth/reauth/passkey/options"){
  let kind=if path.ends_with("/register/options"){"register"}else if path.contains("reauth"){"reauth"}else{"login"};
  let s=if kind=="register"{Some(session(ctx,&["admin","bootstrap"],None)?)}else if kind=="reauth"{Some(session(ctx,&["admin"],None)?)}else{None};
  if kind=="reauth"&&s.as_ref().is_some_and(|s|text(s,"channel")!="browser"){return Err(ApiError::new(403,"请在当前浏览器中确认身份"));}
  throttle(ctx,if kind=="login"{"passkey"}else{"passkey-reauth"},20,60.0,None)?;
  let core=webauthn(ctx)?;
  let value=if kind=="register"{
   let s=s.as_ref().ok_or_else(denied)?;recent(s)?;
   let sid=s["shop_id"].as_str();let user_id=sid.map(|id|format!("extore-shop:{id}")).unwrap_or_else(||"extore-owner".into());
   let user_name=if let Some(sid)=sid{let own=shop(ctx,sid)?;let email=text(&own,"email");if email.is_empty(){sid.to_owned()}else{email.to_owned()}}else{"owner".into()};
   let exclude=rows(&ctx.conn,"SELECT id FROM credentials WHERE shop_id IS ?",[sid])?.iter().map(|v|URL_SAFE_NO_PAD.decode(text(v,"id")).map(Into::into).map_err(|_|invalid())).collect::<Result<Vec<_>>>()?;
   let builder=core.new_challenge_register_builder(user_id.as_bytes(),&user_name,&user_name).map_err(|_|invalid())?.user_verification_policy(UserVerificationPolicy::Required).require_resident_key(true).reject_synchronised_authenticators(false).exclude_credentials(Some(exclude));
   let (options,state)=core.generate_challenge_register(builder).map_err(|_|invalid())?;
   save_challenge(ctx,kind,Some(s),&state,serde_json::to_value(options)?["publicKey"].clone())?
  }else{
   let credentials=if let Some(s)=&s{rows(&ctx.conn,"SELECT * FROM credentials WHERE shop_id IS ?",[s["shop_id"].as_str()])?.iter().map(|v|credential(ctx,v)).collect::<Result<Vec<_>>>()?}else{Vec::new()};
   if kind=="reauth"&&credentials.is_empty(){return Err(ApiError::new(409,"当前账号没有 Passkey，请使用其他验证方式"));}
   let builder=core.new_challenge_authenticate_builder(credentials,Some(UserVerificationPolicy::Required)).map_err(|_|invalid())?.allow_backup_eligible_upgrade(true);
   let (options,state)=core.generate_challenge_authenticate(builder).map_err(|_|invalid())?;
   save_challenge(ctx,kind,s.as_ref(),&state,serde_json::to_value(options)?["publicKey"].clone())?
  };
  return Ok(Some(Reply::json(value)));
 }
 if ctx.is("POST","/api/auth/register/verify"){
  let s=session(ctx,&["admin","bootstrap"],None)?;recent(&s)?;
  let state:RegistrationState=consume(ctx,"register",Some(&s))?;
  let response:RegisterPublicKeyCredential=serde_json::from_value(ctx.body["credential"].clone()).map_err(|_|invalid())?;
  let verified=webauthn(ctx)?.register_credential(&response,&state,None).map_err(|_|invalid())?;
  let cid=URL_SAFE_NO_PAD.encode(verified.cred_id.as_slice());
  if row(&ctx.conn,"SELECT 1 FROM credentials WHERE id=?",[&cid])?.is_some(){return Err(ApiError::new(409,"此 Passkey 已存在"));}
  let name=if ctx.b("name").is_empty(){"我的 Passkey"}else{ctx.b("name")};
  if name.chars().count()>100{return Err(ApiError::bad("Passkey 名称过长"));}
  ctx.conn.execute("INSERT INTO credentials(id,public_key,sign_count,name,created,shop_id) VALUES (?,?,?,?,?,?)",params![cid,cose_bytes(&verified.cred)?,verified.counter,name,now(),s["shop_id"].as_str()])?;
  set_setting(&ctx.conn,&format!("passkey-metadata:{cid}"),&json!({"backup_eligible":verified.backup_eligible,"backup_state":verified.backup_state}).to_string())?;
  if s["shop_id"].is_null(){set_setting(&ctx.conn,"bootstrap_password","")?;}
  if text(&s,"role")=="bootstrap"{revoke_shop_auth(ctx,None)?;create_session(ctx,"admin",None,None,"passkey")?;}
  audit(&ctx.conn,&actor(&s),"passkey.add",&cid,s["shop_id"].as_str())?;
  ctx.set_cookie("extore_challenge=; Path=/api/auth; HttpOnly; SameSite=Strict; Max-Age=0")?;
  if s["shop_id"].is_null(){let path=ctx.app.data.join("bootstrap-password.txt");if path.exists(){std::fs::remove_file(path)?;}}
  return Ok(Some(Reply::json(json!({"ok":true}))));
 }
 if ctx.method.as_str()=="POST"&&matches!(path.as_str(),"/api/auth/login/verify"|"/api/auth/reauth/passkey/verify"){
  let reauth=path.contains("reauth");let s=if reauth{Some(session(ctx,&["admin"],None)?)}else{None};
  if s.as_ref().is_some_and(|s|text(s,"channel")!="browser"){return Err(ApiError::new(403,"请在当前浏览器中确认身份"));}
  throttle(ctx,if reauth{"passkey-reauth-verify"}else{"passkey-verify"},20,60.0,None)?;
  let mut state:AuthenticationState=consume(ctx,if reauth{"reauth"}else{"login"},s.as_ref())?;
  let input=ctx.body["credential"].clone();let cid=text(&input,"id");
  let stored=row(&ctx.conn,"SELECT * FROM credentials WHERE id=?",[cid])?.ok_or_else(||ApiError::new(401,"Passkey 未注册"))?;
  if s.as_ref().is_some_and(|s|s["shop_id"]!=stored["shop_id"]){return Err(ApiError::new(401,"所选 Passkey 不属于当前账号，请重新选择"));}
  if let Some(sid)=stored["shop_id"].as_str(){shop(ctx,sid)?;}
  if let Some(handle)=input["response"]["userHandle"].as_str(){
   let expected=stored["shop_id"].as_str().map(|id|format!("extore-shop:{id}")).unwrap_or_else(||"extore-owner".into());
   if URL_SAFE_NO_PAD.decode(handle).map_err(|_|invalid())?!=expected.as_bytes(){return Err(invalid());}
  }
  state.set_allowed_credentials(vec![credential(ctx,&stored)?]);
  let response:PublicKeyCredential=serde_json::from_value(input).map_err(|_|invalid())?;
  let verified=webauthn(ctx)?.authenticate_credential(&response,&state).map_err(|_|invalid())?;
  if !verified.user_verified(){return Err(invalid());}
  let cid=text(&stored,"id");
  ctx.conn.execute("UPDATE credentials SET sign_count=? WHERE id=?",params![verified.counter(),cid])?;
  set_setting(&ctx.conn,&format!("passkey-metadata:{cid}"),&json!({"backup_eligible":verified.backup_eligible(),"backup_state":verified.backup_state()}).to_string())?;
  let value=if let Some(s)=s{
   ctx.conn.execute("UPDATE sessions SET auth_at=?,auth_method='passkey' WHERE digest=?",params![now(),text(&s,"digest")])?;
   audit(&ctx.conn,&actor(&s),"account.reauthenticate",text(&s,"id"),s["shop_id"].as_str())?;json!({"ok":true})
  }else{create_session(ctx,"admin",stored["shop_id"].as_str(),None,"passkey")?;json!({"role":"admin","shop_id":stored["shop_id"],"superadmin":stored["shop_id"].is_null()})};
  ctx.set_cookie("extore_challenge=; Path=/api/auth; HttpOnly; SameSite=Strict; Max-Age=0")?;return Ok(Some(Reply::json(value)));
 }
 if ctx.is("GET","/api/auth/passkeys"){
  let s=session(ctx,&["admin"],None)?;return Ok(Some(Reply::json(json!(rows(&ctx.conn,"SELECT id,name,created FROM credentials WHERE shop_id IS ?",[s["shop_id"].as_str()])?))));
 }
 if ctx.method.as_str()=="DELETE"&&path.starts_with("/api/auth/passkeys/"){
  let s=session(ctx,&["admin"],None)?;recent(&s)?;let cid=path.trim_start_matches("/api/auth/passkeys/");
  required(&ctx.conn,"SELECT id FROM credentials WHERE id=? AND shop_id IS ?",params![cid,s["shop_id"].as_str()],"Passkey 不存在")?;
  let count=required(&ctx.conn,"SELECT count(*) n FROM credentials WHERE shop_id IS ?",[s["shop_id"].as_str()],"Passkey 不存在")?;
  let password=if let Some(sid)=s["shop_id"].as_str(){!text(&shop(ctx,sid)?,"password_hash").is_empty()}else{false};
  if integer(&count,"n",0)<=1&&!password{return Err(ApiError::new(409,"不能删除最后一个 Passkey；丢失时使用服务器命令重置"));}
  ctx.conn.execute("DELETE FROM credentials WHERE id=?",[cid])?;
  ctx.conn.execute("DELETE FROM settings WHERE key=?",[format!("passkey-metadata:{cid}")])?;
  audit(&ctx.conn,&actor(&s),"passkey.remove",cid,s["shop_id"].as_str())?;
  return Ok(Some(Reply::json(json!({"ok":true}))));
 }
 Ok(None)
}
