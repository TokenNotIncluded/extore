use super::*;
fn slogan(v: &Value) -> Result<String> {
    let s = v
        .as_str()
        .ok_or_else(|| ApiError::new(422, "标语必须是文本，最多四千字符"))?;
    if s.chars().count() > 4000
        || s.chars()
            .any(|c| c.is_control() && !['\t', '\r', '\n'].contains(&c))
    {
        return Err(ApiError::new(422, "标语不能包含控制字符，最多四千字符"));
    }
    Ok(if s.trim().is_empty() {
        String::new()
    } else {
        s.into()
    })
}
pub(super) fn handle(ctx: &mut Context) -> Result<Option<Reply>> {
    if ctx.path == "/api/admin/factory" && (ctx.method == "GET" || ctx.method == "PUT") {
        let s = auth::session(ctx, &["admin"], None)?;
        let sid = auth::selected_shop(ctx, &s, ctx.q("shop_id"))?
            .ok_or_else(|| ApiError::bad("请先选择要配置的店铺"))?;
        if ctx.method == "PUT" {
            auth::cli::fields(ctx, &["factory_slogan"])?;
            let value = slogan(&ctx.body["factory_slogan"])?;
            ctx.conn.execute(
                "UPDATE shops SET factory_slogan=? WHERE id=?",
                params![value, sid],
            )?;
            audit(
                &ctx.conn,
                &actor(&s),
                "factory.slogan.update",
                &sid,
                Some(&sid),
            )?;
        }
        let shop = auth::shop(ctx, &sid)?;
        return Ok(Some(Reply::json(
            json!({"shop_id":shop["id"],"shop_name":shop["name"],"factory_slogan":slogan(&shop["factory_slogan"])?}),
        )));
    }
    if ctx.is("PUT", "/api/manage/workshop") || ctx.is("GET", "/api/manage/instructions") {
        let update = ctx.method == "PUT";
        let s = auth::session(
            ctx,
            &["admin", "staff"],
            if update { Some("product.edit") } else { None },
        )?;
        let mut pid = ctx.q("product_id").to_owned();
        if text(&s, "role") == "staff" {
            if !pid.is_empty() && pid != text(&s, "product_id") {
                return Err(ApiError::new(403, "没有此商品的管理权限"));
            }
            pid = text(&s, "product_id").into();
            if !update
                && !auth::permission_list(&s["permissions"])?
                    .iter()
                    .any(|p| ["queue.view", "queue.monitor", "product.edit"].contains(&p.as_str()))
            {
                return Err(ApiError::new(403, "此商品管理链接没有查看车间标语的权限"));
            }
        }
        if pid.is_empty() {
            return Err(ApiError::bad("请选择商品车间"));
        }
        auth::authorize_product(ctx, &s, &pid)?;
        if update {
            auth::cli::fields(ctx, &["workshop_slogan"])?;
            catalog::require_active(ctx, &pid)?;
            let value = slogan(&ctx.body["workshop_slogan"])?;
            ctx.conn.execute(
                "UPDATE products SET config=json_set(config,'$.workshop_slogan',?) WHERE id=?",
                params![value, pid],
            )?;
            audit(
                &ctx.conn,
                &actor(&s),
                "workshop.slogan.update",
                &pid,
                s["shop_id"].as_str(),
            )?;
            return Ok(Some(Reply::json(
                json!({"product_id":pid,"workshop_slogan":value}),
            )));
        }
        return Ok(Some(Reply::json(catalog::instructions(ctx, &pid)?)));
    }
    Ok(None)
}
