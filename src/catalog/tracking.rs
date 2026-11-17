use super::{cards,product};
use crate::common::*;
use rusqlite::{params,named_params};
use serde_json::{Value,json};
pub fn stats(ctx:&Context,pid:&str,sid:&str)->Result<Value> {
    let mut products=Vec::new();let mut summary=cards::empty_summary();
    for raw in rows(&ctx.conn,"SELECT id FROM products WHERE (?='' OR id=?) AND (?='' OR shop_id=?) ORDER BY created,id",params![pid,pid,sid,sid])? {
        let id=text(&raw,"id");let p=product(ctx,id)?;let mut metadata=std::collections::BTreeMap::new();
        for v in p["variants"].as_array().unwrap(){metadata.insert(text(v,"id").to_owned(),v.clone());}
        for item in rows(&ctx.conn,"SELECT DISTINCT COALESCE(NULLIF(m.variant_id,''),'default') AS id FROM cards c LEFT JOIN card_meta m ON m.card_id=c.id WHERE c.product_id=?",[id])? {let vid=text(&item,"id");if !metadata.contains_key(vid){let r=row(&ctx.conn,"SELECT m.variant_snapshot FROM cards c LEFT JOIN card_meta m ON m.card_id=c.id WHERE c.product_id=? AND COALESCE(NULLIF(m.variant_id,''),'default')=? ORDER BY c.created,c.id LIMIT 1",params![id,vid])?;let mut v=json!({"id":vid,"name":if vid=="default"{"默认规格"}else{vid},"description":"","price":null,"currency":"CNY","enabled":false});if let Some(r)=r{let snap=json_field(&r,"variant_snapshot",json!({}));for k in ["name","description","price","currency"]{if snap.get(k).is_some(){v[k]=snap[k].clone();}}}metadata.insert(vid.to_owned(),v);}}
        let mut variants=Vec::new();for (vid,v) in metadata{variants.push(json!({"variant_id":vid,"name":v["name"],"description":v["description"],"price":v["price"],"currency":v["currency"],"enabled":flag(&v,"enabled"),"summary":cards::summary_for(ctx,id,&vid,sid)?}));}
        let sub=cards::summary_for(ctx,id,"",sid)?;for (k,v) in sub.as_object().unwrap(){if k=="states"{for (k,v) in v.as_object().unwrap(){summary["states"][k]=json!(integer(&summary["states"],k,0)+v.as_i64().unwrap_or(0));}}else{summary[k]=json!(integer(&summary,k,0)+v.as_i64().unwrap_or(0));}}
        let mut view=sub;view["product_id"]=json!(id);view["product_name"]=p["name"].clone();view["variants"]=json!(variants);products.push(view);
    }
    Ok(json!({"summary":summary,"variants":if !pid.is_empty(){products.first().map(|p|p["variants"].clone()).unwrap_or(json!([]))}else{json!([])},"products":products}))
}
pub fn history(ctx:&Context,cid:&str,pid:&str)->Result<Value> {
    let c=required(&ctx.conn,&format!("{} SELECT * FROM inventory WHERE id=:cid",cards::INVENTORY),named_params!{":product_id":pid,":shop_id":"",":now":now(),":cid":cid},"卡密不存在")?;
    let mut timeline=vec![json!({"id":format!("issued:{cid}"),"type":"card.issued","created":c["created"]})];
    if !c["first_verified"].is_null(){timeline.push(json!({"id":format!("verified:{cid}"),"type":"card.verified","created":c["first_verified"]}));}
    if !text(&c,"job_id").is_empty(){for e in rows(&ctx.conn,"SELECT id,type,created,payload FROM events WHERE job_id=? AND product_id=? ORDER BY created,id",params![text(&c,"job_id"),pid])?{let p=json_field(&e,"payload",json!({}));let data=&p["data"];let mut v=json!({"id":e["id"],"type":e["type"],"created":e["created"]});for (k,max) in [("attempt",1000000),("progress",100)]{if data[k].as_i64().is_some_and(|n|(0..=max).contains(&n)){v[k]=data[k].clone();}}if matches!(text(data,"state"),"queued"|"processing"|"succeeded"|"failed"|"needs_input"|"rejected"|"destroyed"){v["state"]=data["state"].clone();}if text(&v,"type")=="redemption.requested"&&integer(&v,"attempt",1)>1{v["type"]=json!("redemption.retried");}timeline.push(v);}}
    for a in rows(&ctx.conn,"SELECT id,created FROM audit WHERE action='card.revoke' AND target=?",[cid])?{timeline.push(json!({"id":format!("revoked:{}",integer(&a,"id",0)),"type":"card.revoked","created":a["created"]}));}
    timeline.sort_by(|a,b|number(a,"created").total_cmp(&number(b,"created")).then_with(||text(a,"id").cmp(text(b,"id"))));Ok(json!({"card":c,"timeline":timeline}))
}
