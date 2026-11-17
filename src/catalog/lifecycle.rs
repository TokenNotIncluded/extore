use crate::{auth, common::*};
use rusqlite::params;
use serde_json::{Value, json};
use std::collections::BTreeSet;

pub fn handle(ctx: &mut Context) -> Result<Option<Reply>> {
    let admin = ctx.path.starts_with("/api/admin/products");
    let managed = ctx.path.starts_with("/api/manage/product");
    if !admin && !managed {
        return Ok(None);
    }
    let empty = matches!(
        ctx.path.as_str(),
        "/api/admin/products/empty-trash" | "/api/manage/products/empty-trash"
    ) && ctx.method.as_str() == "POST";
    let suffix = ctx.path.strip_prefix("/api/admin/products/").unwrap_or("");
    let parts = suffix.split('/').collect::<Vec<_>>();
    let action = if empty || ctx.path.ends_with("/purge") && ctx.method.as_str() == "POST" {
        "purge"
    } else if ctx.path.ends_with("/restore") && ctx.method.as_str() == "POST" {
        "restore"
    } else if ctx.method.as_str() == "DELETE"
        && (ctx.path == "/api/manage/product" || admin && parts.len() == 1 && !parts[0].is_empty())
    {
        "delete"
    } else {
        return Ok(None);
    };
    let session = auth::session(
        ctx,
        if admin {
            &["admin"]
        } else {
            &["admin", "staff"]
        },
        Some(if action == "purge" {
            "product.purge"
        } else {
            "product.delete"
        }),
    )?;
    if action != "restore"
        && (ctx.body["confirmed"] != json!(true)
            || ctx.body.as_object().is_none_or(|o| {
                o.keys()
                    .any(|k| k != "confirmed" && !(empty && k == "product_ids"))
            }))
    {
        return Err(ApiError::new(422, "删除商品需要明确确认且不能包含额外字段"));
    }
    if text(&session, "role") == "staff"
        && !ctx.q("product_id").is_empty()
        && ctx.q("product_id") != text(&session, "product_id")
    {
        return Err(ApiError::new(403, "没有此商品权限"));
    }
    let pids = if empty {
        let a = ctx.body["product_ids"]
            .as_array()
            .ok_or_else(|| ApiError::new(422, "请选择商品"))?;
        if a.is_empty() || a.len() > 500 {
            return Err(ApiError::new(422, "请选择 1–500 个商品"));
        }
        let mut seen = BTreeSet::new();
        for p in a {
            let p = p
                .as_str()
                .ok_or_else(|| ApiError::new(422, "商品编号无效"))?;
            if p.is_empty() || p.len() > 100 || !seen.insert(p.to_owned()) {
                return Err(ApiError::new(422, "请选择不重复的商品编号"));
            }
        }
        a.iter()
            .map(|v| v.as_str().unwrap().to_owned())
            .collect::<Vec<_>>()
    } else if admin {
        vec![parts[0].to_owned()]
    } else {
        let own = text(&session, "product_id");
        vec![
            if own.is_empty() {
                ctx.q("product_id")
            } else {
                if !ctx.q("product_id").is_empty() && ctx.q("product_id") != own {
                    return Err(ApiError::new(403, "没有此商品权限"));
                }
                own
            }
            .to_owned(),
        ]
    };
    let shop = auth::selected_shop(ctx, &session, ctx.q("shop_id"))?;
    let mut products = Vec::new();
    for pid in &pids {
        auth::authorize_product(ctx, &session, pid)?;
        let p = super::product(ctx, pid)?;
        if shop.as_ref().is_some_and(|s| s != text(&p, "shop_id")) {
            return Err(ApiError::new(403, "商品不属于所选店铺"));
        }
        if action == "restore" && flag(&p, "purged") {
            return Err(ApiError::new(409, "商品已永久移出回收站，不能恢复"));
        }
        if action == "purge" && !flag(&p, "deleted") {
            return Err(ApiError::new(409, "只能清空已删除的商品，请重新确认回收站"));
        }
        products.push(p);
    }
    if empty
        && products
            .iter()
            .map(|p| text(p, "shop_id"))
            .collect::<BTreeSet<_>>()
            .len()
            > 1
    {
        return Err(ApiError::new(403, "一次只能清空同一店铺的回收站"));
    }
    for p in &products {
        let pid = text(p, "id");
        let sid = text(p, "shop_id");
        match action {
            "restore" => {
                if flag(p, "deleted") {
                    ctx.conn.execute("UPDATE product_lifecycle SET deleted_at=NULL,deleted_by=NULL WHERE product_id=?",[pid])?;
                    audit(
                        &ctx.conn,
                        &actor(&session),
                        "product.restore",
                        pid,
                        Some(sid),
                    )?;
                }
            }
            "delete" => {
                if !flag(p, "deleted") {
                    ctx.conn.execute("INSERT INTO product_lifecycle VALUES (?,?,?) ON CONFLICT(product_id) DO UPDATE SET deleted_at=excluded.deleted_at,deleted_by=excluded.deleted_by",params![pid,now(),actor(&session)])?;
                    audit(
                        &ctx.conn,
                        &actor(&session),
                        "product.delete",
                        pid,
                        Some(sid),
                    )?;
                }
            }
            "purge" => {
                if !flag(p, "purged") {
                    ctx.conn.execute(
                        "INSERT INTO product_purges VALUES (?,?,?)",
                        params![pid, now(), actor(&session)],
                    )?;
                    audit(&ctx.conn, &actor(&session), "product.purge", pid, Some(sid))?;
                }
            }
            _ => unreachable!(),
        }
    }
    let result = if empty {
        json!({"ok":true,"purged_product_ids":pids,"purged_count":pids.len(),"preserved_fulfillment":true})
    } else {
        let p = super::product(ctx, &pids[0])?;
        let mut v = json!({"ok":true,"product_id":pids[0],"deleted":flag(&p,"deleted"),"deleted_at":p["deleted_at"]});
        if action == "purge" {
            v.as_object_mut().unwrap().remove("deleted_at");
            v["purged"] = json!(true);
            v["purged_at"] = p["purged_at"].clone();
        }
        v
    };
    Ok(Some(Reply::json(result)))
}
