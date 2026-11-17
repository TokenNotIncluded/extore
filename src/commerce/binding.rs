use super::*;

fn view(value:&Value)->Value{let mut v=json!({});for k in ["id","shop_id","label","created","expires","revoked"]{v[k]=value[k].clone();}v}
pub(super) fn handle(ctx:&mut Context)->CommerceResult<Option<Reply>>{
    if ctx.is("GET","/api/admin/commerce/bindings"){let s=browser(ctx,false)?;let shop=catalog::selected_shop(ctx,&s,ctx.q("shop_id"))?;let values=rows(&ctx.conn,"SELECT * FROM commerce_bindings WHERE shop_id=? ORDER BY created DESC,id",[shop])?.iter().map(view).collect::<Vec<_>>();return Ok(Some(Reply::json(json!({"bindings":values}))));}
    if ctx.is("POST","/api/admin/commerce/bindings"){
        let s=browser(ctx,true)?;let shop=catalog::selected_shop(ctx,&s,ctx.q("shop_id"))?;let days=integer(&ctx.body,"valid_days",365);if !(1..=1095).contains(&days)||ctx.b("label").trim().is_empty()||ctx.b("label").chars().count()>100{return Err(error("invalid_request","绑定名称或有效期无效",400));}
        let count=ctx.conn.query_row("SELECT COUNT(*) FROM commerce_bindings WHERE shop_id=? AND revoked=0 AND expires>?",params![shop,now()],|r|r.get::<_,i64>(0))?;if count>=50{return Err(error("binding_limit","请先撤销不再使用的店铺绑定",409));}
        let id=id();let secret=format!("extore_bind_v1_{}",token());let time=now();ctx.conn.execute("INSERT INTO commerce_bindings VALUES (?,?,?,?,?,?,0)",params![id,shop,digest(&secret),ctx.b("label"),time,time+days as f64*86400.0])?;audit(&ctx.conn,&actor(&s),"commerce.binding.create",&id,Some(&shop))?;let value=required(&ctx.conn,"SELECT * FROM commerce_bindings WHERE id=?",[&id],"绑定不存在")?;return Ok(Some(Reply::json(json!({"binding":view(&value),"binding_id":secret}))));
    }
    if ctx.path.starts_with("/api/admin/commerce/bindings/")&&ctx.method==axum::http::Method::DELETE{let s=browser(ctx,true)?;let shop=catalog::selected_shop(ctx,&s,ctx.q("shop_id"))?;let id=ctx.path.rsplit('/').next().unwrap();if ctx.conn.execute("UPDATE commerce_bindings SET revoked=1 WHERE id=? AND shop_id=?",params![id,shop])?!=1{return Err(error("binding_not_found","绑定不存在",404));}audit(&ctx.conn,&actor(&s),"commerce.binding.revoke",id,Some(&shop))?;return Ok(Some(Reply::json(json!({"ok":true}))));}
    if ctx.is("GET","/api/integrations/commerce/binding/products"){
        if !ctx.cookie("extore_session").is_empty(){return Err(error("invalid_request","商城接口不能混用浏览器登录",401));}
        let raw=ctx.header("authorization").strip_prefix("Bearer ").ok_or_else(||error("invalid_token","店铺绑定无效",401))?;if !raw.starts_with("extore_bind_v1_"){return Err(error("invalid_token","店铺绑定无效",401));}
        let b=row(&ctx.conn,"SELECT b.* FROM commerce_bindings b JOIN shops s ON s.id=b.shop_id WHERE b.key_digest=? AND b.revoked=0 AND b.expires>? AND s.enabled=1",params![digest(raw),now()])?.ok_or_else(||error("invalid_token","店铺绑定无效、过期或已撤销",401))?;
        let shop=required(&ctx.conn,"SELECT id,name FROM shops WHERE id=?",[text(&b,"shop_id")],"店铺不存在")?;let rows=rows(&ctx.conn,"SELECT id FROM products WHERE shop_id=? AND id>? ORDER BY id LIMIT 101",params![text(&b,"shop_id"),ctx.q("after")])?;let mut products=Vec::new();for item in rows.iter().take(100){let pid=text(item,"id");let p=catalog::product(ctx,pid)?;if !flag(&p,"deleted"){products.push(listing::listing(ctx,pid,true)?);}}
        return Ok(Some(Reply::json(json!({"schema":"extore.shop-catalog.v1","issuer":ctx.app.origin,"shop":shop,"binding":view(&b),"products":products,"next_cursor":if rows.len()>100{text(&rows[99],"id")}else{""}}))));
    }
    Ok(None)
}
