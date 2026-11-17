use crate::{common::*,catalog};
use serde_json::{Value,json};

pub fn canonical_ascii(value:&Value)->String {
    let text=value.to_string();let mut result=String::with_capacity(text.len());
    for c in text.chars(){if c.is_ascii(){result.push(c);}else{let mut units=[0u16;2];for unit in c.encode_utf16(&mut units){use std::fmt::Write;write!(result,"\\u{unit:04x}").unwrap();}}}result
}
fn secret_name(s:&str)->bool {
    let mut normalized=String::new();let mut previous=false;for c in s.chars(){if c.is_ascii_uppercase()&&previous{normalized.push('_');}normalized.push(c.to_ascii_lowercase());previous=c.is_ascii_lowercase()||c.is_ascii_digit();}
    matches!(normalized.as_str(),"api"|"key"|"jwt"|"sig"|"auth"|"apikey"|"card_code"|"redemption_code")||normalized.split(['_','-']).any(|s|matches!(s,"key"|"token"|"secret"|"password"|"signature"|"credential"|"credentials"|"authorization"))
}
fn safe_url(s:&str)->bool {
    if s.is_empty()||s.len()>2000||s.chars().any(|c|c.is_control()||c.is_whitespace()){return false;}
    url::Url::parse(s).is_ok_and(|u|u.scheme()=="https"&&u.host_str().is_some()&&u.username().is_empty()&&u.password().is_none()&&u.fragment().is_none()&&!u.query_pairs().any(|(k,_)|secret_name(&k)))
}
pub fn safe_variants(values:&Value)->Value {
    json!(values.as_array().into_iter().flatten().take(100).map(|v|{
        let mut out=json!({"attributes":{},"price":Value::Null});
        for (key,max) in [("id",40),("name",120),("description",10000),("currency",5)]{if let Some(s)=v[key].as_str().filter(|s|s.chars().count()<=max){out[key]=json!(s);}}
        if v["enabled"].is_boolean(){out["enabled"]=v["enabled"].clone();}
        if v["price"].is_string(){out["price"]=v["price"].clone();}
        if let Some(attrs)=v["attributes"].as_object(){for (k,val) in attrs.iter().filter(|(k,v)|!secret_name(k)&&!matches!(k.as_str(),"__proto__"|"constructor"|"prototype")&&k.chars().count()<=100&&(!v.is_array()&&!v.is_object())).take(20){out["attributes"][k]=val.clone();}}
        out
    }).collect::<Vec<_>>())
}
fn fields(values:&Value)->Value {
    json!(values.as_array().into_iter().flatten().take(30).map(|f|{let mut out=json!({});for k in ["key","label","description","type","required","collapsed","options","max_items"]{if let Some(v)=f.get(k){out[k]=v.clone();}}out}).collect::<Vec<_>>())
}
pub fn listing(ctx:&Context,pid:&str,marketplace:bool)->Result<Value> {
    let p=catalog::product(ctx,pid)?;let mut safe=json!({});
    for k in ["name","description","public","mode","delivery","view_policy","support_email","revision_policy"]{if let Some(v)=p.get(k){safe[k]=v.clone();}}
    for k in ["logo","image"]{if safe_url(text(&p,k)){safe[k]=p[k].clone();}}
    safe["parameters"]=fields(&p["parameters"]);safe["outputs"]=fields(&p["outputs"]);
    safe["progress_steps"]=json!(p["progress_steps"].as_array().into_iter().flatten().map(|v|json!({"id":v["id"],"label":v["label"]})).collect::<Vec<_>>());
    let mut out=json!({"schema":"extore.product-listing.v1","id":pid,"shop_id":p["shop_id"],"redemption_url":format!("{}/",ctx.app.origin),"semantics":{"price":"reference","inventory":"not_exported","payment":"external_sales_platform","redemption":"extore"},"product":safe,"variants":safe_variants(&p["variants"])});
    out["revision"]=json!(digest(&out.to_string()));
    if marketplace {
        let mut mp=catalog::marketplace_defaults();if let Some(o)=p["marketplace"].as_object(){mp.as_object_mut().unwrap().extend(o.clone());}out["product"]["marketplace"]=mp;
        for (i,v) in out["variants"].as_array_mut().unwrap().iter_mut().enumerate(){let mut vm=catalog::variant_marketplace_defaults();if let Some(o)=p["variants"][i]["marketplace"].as_object(){vm.as_object_mut().unwrap().extend(o.clone());}v["marketplace"]=vm;}
        let mut sync=out.clone();sync.as_object_mut().unwrap().remove("revision");out["sync_revision"]=json!(digest(&canonical_ascii(&sync)));
        for variant in out["variants"].as_array_mut().unwrap(){variant["card_summary"]=catalog::card_summary(ctx,pid,text(variant,"id"))?;}
        out["card_summary"]=catalog::card_summary(ctx,pid,"")?;
    }
    Ok(out)
}
pub fn metadata(ctx:&Context)->Value {
    let o=&ctx.app.origin;json!({"issuer":o,"authorization_endpoint":format!("{o}/oauth/authorize"),"token_endpoint":format!("{o}/api/integrations/commerce/token"),"revocation_endpoint":format!("{o}/api/integrations/commerce/revoke"),"response_types_supported":["code"],"response_modes_supported":["query"],"grant_types_supported":["authorization_code","refresh_token"],"token_endpoint_auth_methods_supported":["none"],"revocation_endpoint_auth_methods_supported":["none"],"code_challenge_methods_supported":["S256"],"scopes_supported":["products.read","cards.issue","marketplace.sync"],"authorization_response_iss_parameter_supported":true,"extore_commerce":{"schema":"extore.commerce-import.v1","schema_uri":format!("{o}/api/integrations/commerce/schema"),"products_endpoint":format!("{o}/api/integrations/commerce/products"),"cards_endpoint":format!("{o}/api/integrations/commerce/cards"),"listing_schema":"extore.product-listing.v1","marketplace_client_id":"lmm-best-store","maximum_products":100,"maximum_cards_per_request":100,"issuance_recovery_seconds":86400}})
}
