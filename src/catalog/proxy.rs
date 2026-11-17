//! Immutable Ed25519 issuer pins, compatible with every previously issued EXR1 card.
use crate::{auth, common::*};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use ed25519_dalek::{Signer, SigningKey, VerifyingKey, Signature};
use rusqlite::params;
use serde_json::{Value, json};
use std::{collections::BTreeSet, net::ToSocketAddrs};

fn hex_id(s:&str)->bool { s.len()==32 && s.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b)) }
fn decode<const N:usize>(s:&str)->Result<[u8;N]> { let raw=URL_SAFE_NO_PAD.decode(s).map_err(|_|ApiError::bad("签名卡密无效"))?;if URL_SAFE_NO_PAD.encode(&raw)!=s{return Err(ApiError::bad("签名卡密无效"));} raw.try_into().map_err(|_|ApiError::bad("签名卡密无效")) }
fn secret_valid(s:&str)->bool { s.len()==32&&s.bytes().all(|b|b.is_ascii_uppercase()||(b'2'..=b'7').contains(&b)) }
pub fn public_ip(ip:std::net::IpAddr)->bool {
    use std::net::IpAddr;
    match ip {
        IpAddr::V4(ip)=>{let [a,b,c,_]=ip.octets(); !(a==0||a==10||a==127||a>=224||(a==100&&(64..=127).contains(&b))||(a==169&&b==254)||(a==172&&(16..=31).contains(&b))||(a==192&&(b==168||(b==0&&(c==0||c==2))||(b==88&&c==99)))||(a==198&&(b==18||b==19||(b==51&&c==100)))||(a==203&&b==0&&c==113))},
        IpAddr::V6(ip)=>{let s=ip.segments();if let Some(v4)=ip.to_ipv4_mapped(){return public_ip(IpAddr::V4(v4));} s[0]&0xe000==0x2000 && !(s[0]==0x2001&&(s[1]<0x200||s[1]==0xdb8)) && s[0]!=0x2002 && !(s[0]==0x3fff&&s[1]<0x1000)}
    }
}
pub fn canonical_origin(input:&str,resolve:bool)->Result<String> {
    let bad=||ApiError::bad("路由必须使用固定 HTTPS 公网地址");
    if input.len()>300||input.chars().any(|c|c.is_whitespace()||c.is_control()||c=='\\'||c=='%'){return Err(bad());}
    let u=url::Url::parse(input).map_err(|_|bad())?;
    if u.scheme()!="https"||!u.username().is_empty()||u.password().is_some()||u.query().is_some()||u.fragment().is_some()||u.path()!="/"||u.port()==Some(0){return Err(bad());}
    let host=u.host_str().ok_or_else(bad)?;
    if !host.contains('.')||host.ends_with(".local")||host.ends_with(".localhost")||host.split('.').any(|s|s.is_empty()||s.len()>63||s.starts_with('-')||s.ends_with('-')||!s.bytes().all(|b|b.is_ascii_lowercase()||b.is_ascii_digit()||b==b'-'))||host.rsplit('.').next().is_some_and(|s|s.bytes().all(|b|b.is_ascii_digit())||(s.starts_with("0x")&&s[2..].bytes().all(|b|b.is_ascii_hexdigit()))){return Err(bad());}
    if resolve { let addresses=(host,u.port_or_known_default().unwrap()).to_socket_addrs().map_err(|_|bad())?.collect::<Vec<_>>();if addresses.is_empty()||addresses.iter().any(|a|!public_ip(a.ip())){return Err(bad());} }
    Ok(format!("https://{host}{}",u.port().filter(|p|*p!=443).map(|p|format!(":{p}")).unwrap_or_default()))
}
fn canonical_path(s:&str)->Result<&str> { if !s.starts_with('/')||s.len()>150||s.contains("//")||!s.bytes().all(|c|c.is_ascii_alphanumeric()||b"/_-".contains(&c)){return Err(ApiError::bad("路由路径无效"));}Ok(s) }
fn message(route:&Value,secret:&str)->Result<Vec<u8>> { if !hex_id(text(route,"route_id"))||!hex_id(text(route,"issuer_id"))||!secret_valid(secret)||canonical_origin(text(route,"origin"),false)?!=text(route,"origin"){return Err(ApiError::bad("签名卡密无效"));} Ok(format!("Extore routed code v1\n{}\n{}\n{}\n{}\n{}",text(route,"route_id"),text(route,"issuer_id"),text(route,"origin"),canonical_path(text(route,"path"))?,digest(secret)).into_bytes()) }
fn public_route(r:&Value)->Value { let mut v=json!({});for k in ["route_id","issuer_id","name","origin","path","public_key"]{v[k]=r[k].clone();}v }
fn owner_route(r:&Value)->Value { let mut v=public_route(r);for k in ["shop_id","identity_id","enabled","default_issuer","created","updated","archived"]{v[k]=r[k].clone();}v["kind"]=json!(if text(r,"identity_id").is_empty(){"proxy"}else{"issuer"});v }
fn public_identity(r:&Value)->Value { let mut v=r.clone();v.as_object_mut().unwrap().remove("private_key");v["current"]=json!(flag(r,"current"));v }
fn new_identity(ctx:&Context,sid:&str)->Result<Value> {
    let key=SigningKey::generate(&mut rand::rngs::OsRng);let iid=uuid::Uuid::new_v4().simple().to_string();let public=URL_SAFE_NO_PAD.encode(key.verifying_key().as_bytes());
    let encrypted=auth::secret::store_secret(&ctx.app,Some(sid),"proxy-issuer-ed25519:v1",&iid,&json!(URL_SAFE_NO_PAD.encode(key.to_bytes())))?;
    ctx.conn.execute("UPDATE proxy_identities SET current=0 WHERE shop_id=?",[sid])?;
    ctx.conn.execute("INSERT INTO proxy_identities(id,shop_id,name,public_key,private_key,created,current) VALUES (?,?,'main',?,?,?,1)",params![iid,sid,public,encrypted,now()])?;
    required(&ctx.conn,"SELECT * FROM proxy_identities WHERE id=?",[iid],"发行身份不存在")
}
fn local_default(ctx:&Context,identity:&Value)->Result<Option<Value>> {
    let Ok(origin)=canonical_origin(&ctx.app.origin,false) else{return Ok(None)};
    let sid=text(identity,"shop_id");let existing=row(&ctx.conn,"SELECT * FROM proxy_routes WHERE shop_id=? AND identity_id=? AND origin=? AND path='/' AND enabled=1 ORDER BY default_issuer DESC,created DESC LIMIT 1",params![sid,text(identity,"id"),origin])?;
    if existing.as_ref().is_some_and(|r|flag(r,"default_issuer")&&!flag(r,"archived")){return Ok(existing);}
    ctx.conn.execute("UPDATE proxy_routes SET default_issuer=0,archived=1 WHERE shop_id=? AND identity_id IS NOT NULL",[sid])?;
    let rid=if let Some(r)=existing{let rid=text(&r,"route_id").to_owned();ctx.conn.execute("UPDATE proxy_routes SET default_issuer=1,archived=0,updated=? WHERE shop_id=? AND route_id=?",params![now(),sid,rid])?;rid}else{let rid=uuid::Uuid::new_v4().simple().to_string();ctx.conn.execute("INSERT INTO proxy_routes(route_id,shop_id,name,identity_id,issuer_id,public_key,origin,path,enabled,default_issuer,created,updated,archived) VALUES (?,?,?,?,?,?,?,'/',1,1,?,?,0)",params![rid,sid,text(identity,"name"),text(identity,"id"),text(identity,"id"),text(identity,"public_key"),origin,now(),now()])?;rid};
    row(&ctx.conn,"SELECT * FROM proxy_routes WHERE shop_id=? AND route_id=?",params![sid,rid])
}
pub fn ensure_shop_issuer(ctx:&Context,sid:&str)->Result<()> {
    let identity=if let Some(i)=row(&ctx.conn,"SELECT * FROM proxy_identities WHERE shop_id=? AND current=1",[sid])?{i}else if let Some(i)=row(&ctx.conn,"SELECT i.* FROM proxy_identities i JOIN proxy_routes r ON r.identity_id=i.id AND r.shop_id=i.shop_id WHERE i.shop_id=? AND r.default_issuer=1 AND r.enabled=1 ORDER BY r.created DESC LIMIT 1",[sid])?{ctx.conn.execute("UPDATE proxy_identities SET current=1 WHERE id=?",[text(&i,"id")])?;i}else{new_identity(ctx,sid)?};local_default(ctx,&identity)?;Ok(())
}
pub fn wrap_codes(ctx:&Context,sid:&str,codes:Vec<String>,routed:Option<bool>)->Result<Vec<String>> {
    ensure_shop_issuer(ctx,sid)?;
    if routed==Some(false)&&ctx.app.origin.starts_with("https:"){return Err(ApiError::new(409,"新卡必须使用本店发行标识"));}
    let Some(r)=row(&ctx.conn,"SELECT * FROM proxy_routes WHERE shop_id=? AND default_issuer=1 AND identity_id IS NOT NULL",[sid])?else{if routed==Some(true)||ctx.app.origin.starts_with("https:"){return Err(ApiError::new(409,"请先配置本店的签名发行路由"));}return Ok(codes)};
    if !flag(&r,"enabled")||text(&r,"origin")!=canonical_origin(&ctx.app.origin,false)?||text(&r,"path")!="/"{return Err(ApiError::new(409,"签名发行路由与本站地址不匹配"));}
    let identity=required(&ctx.conn,"SELECT * FROM proxy_identities WHERE shop_id=? AND id=?",params![sid,text(&r,"identity_id")],"发行身份不可用")?;
    let raw=auth::secret::open_secret(&ctx.app,Some(sid),"proxy-issuer-ed25519:v1",text(&identity,"id"),text(&identity,"private_key"))?;
    let key=SigningKey::from_bytes(&decode::<32>(raw.as_str().ok_or_else(||ApiError::new(500,"发行身份不可用"))?)?);
    if text(&r,"public_key")!=URL_SAFE_NO_PAD.encode(key.verifying_key().as_bytes())||r["public_key"]!=identity["public_key"]{return Err(ApiError::new(409,"发行身份不可用"));}
    let mut out=Vec::with_capacity(codes.len());
    for code in codes {let secret=code.replace('-',"");let signature=URL_SAFE_NO_PAD.encode(key.sign(&message(&r,&secret)?).to_bytes());let card=required(&ctx.conn,"SELECT c.id FROM cards c JOIN products p ON p.id=c.product_id WHERE c.digest=? AND p.shop_id=?",params![super::card_digest(&secret),sid],"卡密不属于此店铺")?;let cid=text(&card,"id");if let Some(old)=row(&ctx.conn,"SELECT * FROM proxy_issued_cards WHERE card_id=?",[cid])?{if old["route_id"]!=r["route_id"]||text(&old,"shop_id")!=sid{return Err(ApiError::new(409,"已发行的卡密不能改写签名路由"));}}
        ctx.conn.execute("INSERT INTO proxy_issued_cards(card_id,shop_id,route_id,issued) VALUES (?,?,?,?) ON CONFLICT(card_id) DO NOTHING",params![cid,sid,text(&r,"route_id"),now()])?;out.push(format!("EXR1.{}.{}.{}.{}",text(&r,"route_id"),secret,text(&r,"issuer_id"),signature));}
    Ok(out)
}
pub fn unwrap_code(ctx:&Context,input:&str)->Result<String> {
    let value=input.trim();let upper=value.to_ascii_uppercase();let digits=upper.strip_prefix("EXR").map(|s|s.chars().take_while(|c|c.is_ascii_digit()).collect::<String>()).unwrap_or_default();
    let routed=!digits.is_empty()&&(value.len()>64||digits.bytes().any(|b|b"0189".contains(&b))||value.len()==3+digits.len()||value.get(3+digits.len()..).is_some_and(|s|s.starts_with('.')));
    if !routed{return Ok(input.to_owned());}
    let bad=||ApiError::bad("签名卡密无效或不属于本站");let parts=value.split('.').collect::<Vec<_>>();if value.len()>200||parts.len()!=5||parts[0]!="EXR1"||!hex_id(parts[1])||!hex_id(parts[3])||!secret_valid(parts[2]){return Err(bad());}
    let r=row(&ctx.conn,"SELECT r.* FROM proxy_routes r JOIN shops s ON s.id=r.shop_id WHERE r.route_id=? AND r.enabled=1 AND s.enabled=1 AND r.identity_id IS NOT NULL",[parts[1]])?.ok_or_else(bad)?;
    if text(&r,"issuer_id")!=parts[3]||text(&r,"origin")!=canonical_origin(&ctx.app.origin,false)?||!matches!(text(&r,"path"),"/"|"/proxy"){return Err(bad());}
    if row(&ctx.conn,"SELECT id FROM proxy_identities WHERE id=? AND shop_id=? AND public_key=?",params![text(&r,"identity_id"),text(&r,"shop_id"),text(&r,"public_key")])?.is_none(){return Err(bad());}
    let key=VerifyingKey::from_bytes(&decode::<32>(text(&r,"public_key"))?).map_err(|_|bad())?;key.verify_strict(&message(&r,parts[2])?,&Signature::from_bytes(&decode::<64>(parts[4])?)).map_err(|_|bad())?;
    let card=row(&ctx.conn,"SELECT p.shop_id FROM cards c JOIN products p ON p.id=c.product_id WHERE c.digest=?",[super::card_digest(parts[2])])?.ok_or_else(bad)?;if card["shop_id"]!=r["shop_id"]{return Err(bad());}Ok(parts[2].to_owned())
}
fn issued_count(ctx:&Context,r:&Value)->Result<i64> {
    if text(r,"identity_id").is_empty(){return Ok(0);}
    let cutoff=setting(&ctx.conn,"proxy_tracking_started","0")?.parse::<f64>().unwrap_or(0.0);
    Ok(ctx.conn.query_row("SELECT count(*) FROM cards JOIN products ON products.id=cards.product_id LEFT JOIN card_meta meta ON meta.card_id=cards.id LEFT JOIN jobs j ON j.card_id=cards.id LEFT JOIN proxy_issued_cards issued ON issued.card_id=cards.id WHERE products.shop_id=? AND (cards.state NOT IN ('revoked','rejected') OR (cards.state='revoked' AND meta.batch_previous_state='ready' AND meta.batch_id IS NOT NULL)) AND (j.id IS NULL OR j.state NOT IN ('destroyed','rejected')) AND (j.id IS NULL OR j.state!='succeeded' OR (COALESCE(json_extract(products.config,'$.view_policy'),'repeat')!='once' AND COALESCE(json_extract(products.config,'$.delivery'),'content')!='service')) AND (meta.expires IS NULL OR meta.expires>? OR (j.id IS NOT NULL AND j.state NOT IN ('failed','needs_input'))) AND ((issued.shop_id=? AND issued.route_id=?) OR (issued.card_id IS NULL AND cards.created>=? AND cards.created<=?))",params![text(r,"shop_id"),now(),text(r,"shop_id"),text(r,"route_id"),number(r,"created"),cutoff],|r|r.get(0))?)
}
fn preview_route(ctx:&Context,r:&Value)->Result<Value>{let n=issued_count(ctx,r)?;Ok(json!({"shop_id":r["shop_id"],"route_id":r["route_id"],"current":flag(r,"default_issuer"),"enabled":flag(r,"enabled"),"default_issuer":flag(r,"default_issuer"),"eligible":!flag(r,"default_issuer")&&!flag(r,"enabled")&&n==0,"issued_card_count":n}))}
fn preview_identity(ctx:&Context,r:&Value)->Result<Value>{let routes=rows(&ctx.conn,"SELECT * FROM proxy_routes WHERE shop_id=? AND identity_id=?",params![text(r,"shop_id"),text(r,"id")])?;let mut count=0;for r in &routes{count+=issued_count(ctx,r)?;}Ok(json!({"shop_id":r["shop_id"],"id":r["id"],"current":flag(r,"current"),"eligible":!flag(r,"current")&&count==0&&routes.iter().all(|r|!flag(r,"default_issuer")),"route_count":routes.len(),"issued_card_count":count}))}
pub fn handle(ctx:&mut Context)->Result<Option<Reply>> {
    if ctx.is("GET","/api/proxy/routes"){let mut seen=BTreeSet::new();let data=rows(&ctx.conn,"SELECT r.* FROM proxy_routes r JOIN shops s ON s.id=r.shop_id WHERE r.enabled=1 AND s.enabled=1 ORDER BY r.created,r.route_id",[])?.iter().filter(|r|seen.insert(text(r,"route_id").to_owned())).map(public_route).collect::<Vec<_>>();return Ok(Some(Reply::json(json!(data))));}
    let Some(suffix)=ctx.path.strip_prefix("/api/admin/proxy/").map(str::to_owned)else{return Ok(None)};let s=auth::session(ctx,&["admin"],None)?;let sid=super::selected_shop(ctx,&s,if ctx.b("shop_id").is_empty(){ctx.q("shop_id")}else{ctx.b("shop_id")})?;let method=ctx.method.as_str();let parts=suffix.split('/').collect::<Vec<_>>();let history=matches!(ctx.q("history"),"true"|"1");
    let mut result=None;
    if suffix=="identities" {if method=="GET"{result=Some(json!(rows(&ctx.conn,"SELECT * FROM proxy_identities WHERE shop_id=? AND (? OR current=1) ORDER BY current DESC,created DESC,id",params![sid,history])?.iter().map(public_identity).collect::<Vec<_>>()));}else if method=="POST"{if ctx.b("name").trim().is_empty()||ctx.b("name").chars().count()>100{return Err(ApiError::bad("请输入发行身份名称"));}let i=new_identity(ctx,&sid)?;local_default(ctx,&i)?;audit(&ctx.conn,&actor(&s),"proxy_identity.replace",text(&i,"id"),Some(&sid))?;result=Some(public_identity(&i));}}
    else if suffix=="routes" {if method=="GET"{result=Some(json!(rows(&ctx.conn,"SELECT * FROM proxy_routes WHERE shop_id=? AND (? OR (archived=0 AND enabled=1)) ORDER BY default_issuer DESC,created DESC,route_id",params![sid,history])?.iter().map(owner_route).collect::<Vec<_>>()));}else if method=="POST"{result=Some(create_route(ctx,&sid,&s)?);}}
    else if suffix=="cleanup" && matches!(method,"GET"|"POST") {let mut identities=BTreeSet::new();for i in rows(&ctx.conn,"SELECT * FROM proxy_identities WHERE shop_id=? AND current=0",[&sid])?{if flag(&preview_identity(ctx,&i)?,"eligible"){identities.insert(text(&i,"id").to_owned());}}let mut routes=Vec::new();for r in rows(&ctx.conn,"SELECT * FROM proxy_routes WHERE shop_id=?",[&sid])?{if identities.contains(text(&r,"identity_id"))||flag(&preview_route(ctx,&r)?,"eligible"){routes.push(text(&r,"route_id").to_owned());}}
        if method=="POST" {for r in &routes{ctx.conn.execute("DELETE FROM proxy_routes WHERE shop_id=? AND route_id=?",params![sid,r])?;audit(&ctx.conn,&actor(&s),"proxy_route.delete",r,Some(&sid))?;}for i in &identities{ctx.conn.execute("DELETE FROM proxy_identities WHERE shop_id=? AND id=?",params![sid,i])?;audit(&ctx.conn,&actor(&s),"proxy_identity.delete",i,Some(&sid))?;}result=Some(json!({"ok":true,"shop_id":sid,"deleted_identity_ids":identities,"deleted_route_ids":routes,"deleted_identity_count":identities.len(),"deleted_route_count":routes.len()}));}else{result=Some(json!({"shop_id":sid,"eligible_identity_ids":identities,"eligible_route_ids":routes,"eligible_identity_count":identities.len(),"eligible_route_count":routes.len()}));}}
    else if parts.len()>=2&&matches!(parts[0],"identities"|"routes") {let identity=parts[0]=="identities";let rid=parts[1];let sql=if identity{"SELECT * FROM proxy_identities WHERE shop_id=? AND id=?"}else{"SELECT * FROM proxy_routes WHERE shop_id=? AND route_id=?"};let r=required(&ctx.conn,sql,params![sid,rid],"路由或发行身份不存在")?;
        if parts.get(2)==Some(&"cleanup-preview")&&method=="GET"{result=Some(if identity{preview_identity(ctx,&r)?}else{preview_route(ctx,&r)?});}
        else if parts.len()==2&&method=="DELETE"{let preview=if identity{preview_identity(ctx,&r)?}else{preview_route(ctx,&r)?};if !flag(&preview,"eligible"){return Err(ApiError::new(409,"当前或仍有关联有效卡密的发行身份不能删除；请先停用旧路由"));}if identity{ctx.conn.execute("DELETE FROM proxy_routes WHERE shop_id=? AND identity_id=?",params![sid,rid])?;ctx.conn.execute("DELETE FROM proxy_identities WHERE shop_id=? AND id=?",params![sid,rid])?;}else{ctx.conn.execute("DELETE FROM proxy_routes WHERE shop_id=? AND route_id=?",params![sid,rid])?;}audit(&ctx.conn,&actor(&s),if identity{"proxy_identity.delete"}else{"proxy_route.delete"},rid,Some(&sid))?;let mut v=json!({"ok":true,"deleted":true,"shop_id":sid});v[if identity{"id"}else{"route_id"}]=json!(rid);result=Some(v);}
        else if !identity&&parts.len()==2&&matches!(method,"PUT"|"PATCH"){result=Some(update_route(ctx,&r,&s)?);}
    }
    Ok(result.map(Reply::json))
}
fn create_route(ctx:&Context,sid:&str,s:&Value)->Result<Value> {
    let origin=canonical_origin(ctx.b("origin"),true)?;let path=canonical_path(if ctx.b("path").is_empty(){"/"}else{ctx.b("path")})?;let name=ctx.b("name").trim();if name.is_empty()||name.chars().count()>100{return Err(ApiError::bad("请输入路由名称"));}let default=flag(&ctx.body,"default_issuer");
    let iid=ctx.b("identity_id");let (rid,issuer,public)=if !iid.is_empty(){let i=required(&ctx.conn,"SELECT * FROM proxy_identities WHERE shop_id=? AND id=?",params![sid,iid],"发行身份不属于此店铺")?;if origin!=canonical_origin(&ctx.app.origin,false)?||path!="/"||!ctx.b("issuer_id").is_empty()||!ctx.b("public_key").is_empty(){return Err(ApiError::bad("发行路由必须指向本站，并使用本站发行身份"));}if default&&!flag(&i,"current"){return Err(ApiError::new(409,"请使用本店当前发行标识"));}if flag(&i,"current")&&ctx.b("route_id").is_empty(){let r=local_default(ctx,&i)?.ok_or_else(||ApiError::bad("本站需要 HTTPS"))?;ctx.conn.execute("UPDATE proxy_routes SET name=?,updated=? WHERE shop_id=? AND route_id=?",params![name,now(),sid,text(&r,"route_id")])?;let r=required(&ctx.conn,"SELECT * FROM proxy_routes WHERE shop_id=? AND route_id=?",params![sid,text(&r,"route_id")],"路由不存在")?;audit(&ctx.conn,&actor(s),"proxy_route.update",text(&r,"route_id"),Some(sid))?;return Ok(owner_route(&r));}(if ctx.b("route_id").is_empty(){uuid::Uuid::new_v4().simple().to_string()}else{ctx.b("route_id").to_owned()},text(&i,"id").to_owned(),text(&i,"public_key").to_owned())}else{if default{return Err(ApiError::bad("导入路由不能用作本地发行身份"));}(ctx.b("route_id").to_owned(),ctx.b("issuer_id").to_owned(),ctx.b("public_key").to_owned())};
    if !hex_id(&rid)||!hex_id(&issuer){return Err(ApiError::bad("请输入完整的已确认发行站配置"));}decode::<32>(&public)?;
    for r in rows(&ctx.conn,"SELECT * FROM proxy_routes WHERE route_id=?",[&rid])?{if text(&r,"issuer_id")!=issuer||text(&r,"public_key")!=public||text(&r,"origin")!=origin||text(&r,"path")!=path{return Err(ApiError::new(409,"路由 ID 的发行身份、目标与公钥不能替换"));}if text(&r,"shop_id")==sid{return Err(ApiError::new(409,"本店已绑定此路由，请编辑已有绑定"));}}
    if default{ctx.conn.execute("UPDATE proxy_routes SET default_issuer=0 WHERE shop_id=?",[sid])?;}
    ctx.conn.execute("INSERT INTO proxy_routes(route_id,shop_id,name,identity_id,issuer_id,public_key,origin,path,enabled,default_issuer,created,updated,archived) VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?)",params![rid,sid,name,if iid.is_empty(){None}else{Some(iid)},issuer,public,origin,path,default,now(),now(),!iid.is_empty()&&!default])?;audit(&ctx.conn,&actor(s),"proxy_route.create",&rid,Some(sid))?;Ok(owner_route(&required(&ctx.conn,"SELECT * FROM proxy_routes WHERE shop_id=? AND route_id=?",params![sid,rid],"路由不存在")?))
}
fn update_route(ctx:&Context,r:&Value,s:&Value)->Result<Value> {
    if ctx.body.as_object().is_none_or(|b|b.keys().any(|k|!matches!(k.as_str(),"name"|"enabled"|"default_issuer"))){return Err(ApiError::bad("路由配置字段无效"));}
    for k in ["enabled","default_issuer"]{if ctx.body.get(k).is_some_and(|v|!v.is_null()&&!v.is_boolean()){return Err(ApiError::bad("路由状态无效"));}}
    let name=ctx.body["name"].as_str().unwrap_or(text(r,"name")).trim();if name.is_empty()||name.chars().count()>100{return Err(ApiError::bad("请输入路由名称"));}let enabled=ctx.body["enabled"].as_bool().unwrap_or(flag(r,"enabled"));let default=ctx.body["default_issuer"].as_bool().unwrap_or(flag(r,"default_issuer"));
    if flag(r,"default_issuer")&&(!default||!enabled){return Err(ApiError::new(409,"必须保留当前发行路由；请替换发行标识后再清理旧路由"));}if default{let i=required(&ctx.conn,"SELECT current FROM proxy_identities WHERE id=? AND shop_id=?",params![text(r,"identity_id"),text(r,"shop_id")],"导入路由不能用作本地发行身份")?;if !flag(&i,"current")||!enabled{return Err(ApiError::new(409,"默认发行路由必须属于当前标识且已启用"));}ctx.conn.execute("UPDATE proxy_routes SET default_issuer=0 WHERE shop_id=?",[text(r,"shop_id")])?;}
    ctx.conn.execute("UPDATE proxy_routes SET name=?,enabled=?,default_issuer=?,updated=?,archived=? WHERE shop_id=? AND route_id=?",params![name,enabled,default,now(),if default{false}else{flag(r,"archived")},text(r,"shop_id"),text(r,"route_id")])?;audit(&ctx.conn,&actor(s),"proxy_route.update",text(r,"route_id"),Some(text(r,"shop_id")))?;Ok(owner_route(&required(&ctx.conn,"SELECT * FROM proxy_routes WHERE shop_id=? AND route_id=?",params![text(r,"shop_id"),text(r,"route_id")],"路由不存在")?))
}
