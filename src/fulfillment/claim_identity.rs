//! Snapshot the processing identity after a successful authorized claim.
use super::*;

pub fn record_claim(
    ctx: &Context,
    j: &Value,
    who: &str,
    session: Option<&Value>,
    device_id: Option<&str>,
) -> Result<()> {
    if text(j, "state") != "processing" || text(j, "claimed_by") != who {
        return Err(ApiError::new(409, "处理身份与领取任务不匹配"));
    }
    let did = device_id.filter(|d| !d.is_empty()).or_else(|| {
        session
            .and_then(|s| s["device_id"].as_str())
            .filter(|d| !d.is_empty())
    });
    let (name, agent, channel, device_ref, worker_ref) = if let Some(did) = did {
        let (device, staff) = auth::cli::device(ctx, did)?;
        if text(&staff, "id") != who {
            return Err(ApiError::new(401, "CLI 领取身份已失效"));
        }
        let (name, agent) =
            auth::cli::identity(text(&device, "client_name"), device["agent_type"].as_str())?;
        let binding = row(
            &ctx.conn,
            "SELECT b.authorization_id FROM pipeline_bindings b JOIN pipeline_authorizations a ON a.id=b.authorization_id WHERE b.device_id=? AND b.staff_id=? AND a.public_key=?",
            params![did, who, text(&device, "public_key")],
        )?;
        let worker = if let Some(binding) = binding {
            format!("authorization:{}", text(&binding, "authorization_id"))
        } else {
            format!("device:{did}")
        };
        (name, agent, "cli", Some(did.to_owned()), Some(worker))
    } else if who == "worker" {
        (
            "商品处理器".to_owned(),
            Some("processor".to_owned()),
            "automatic",
            None,
            None,
        )
    } else if session.is_some_and(|s| text(s, "role") == "admin") {
        (
            "店主".to_owned(),
            Some("human".to_owned()),
            "browser",
            None,
            None,
        )
    } else {
        let staff = row(&ctx.conn, "SELECT name FROM staff WHERE id=?", [who])?;
        let name = staff
            .as_ref()
            .map(|s| text(s, "name"))
            .unwrap_or("处理人员")
            .chars()
            .filter(|c| !auth::cli::hidden(*c) && !c.is_control())
            .take(100)
            .collect::<String>();
        let name = if name.trim().is_empty() {
            "处理人员"
        } else {
            name.trim()
        }
        .to_owned();
        (name, Some("human".to_owned()), "browser", None, None)
    };
    ctx.conn.execute("INSERT INTO job_worker_identities(job_id,attempt,actor,device_ref,client_name,agent_type,channel,claimed_at,worker_ref) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(job_id,attempt) DO UPDATE SET actor=excluded.actor,device_ref=excluded.device_ref,client_name=excluded.client_name,agent_type=excluded.agent_type,channel=excluded.channel,claimed_at=excluded.claimed_at,worker_ref=excluded.worker_ref",params![text(j,"id"),integer(j,"attempt",1),who,device_ref,name,agent,channel,now(),worker_ref])?;
    Ok(())
}

pub fn processing_worker(ctx: &Context, j: &Value) -> Result<Value> {
    let Some(identity) = row(
        &ctx.conn,
        "SELECT client_name,agent_type,channel FROM job_worker_identities WHERE job_id=? AND attempt=? AND actor=?",
        params![
            text(j, "id"),
            integer(j, "attempt", 1),
            text(j, "claimed_by")
        ],
    )?
    else {
        return Ok(Value::Null);
    };
    let kind = match text(&identity, "channel") {
        "cli" => "cli",
        "browser" => "human",
        "automatic" => "automatic",
        _ => return Err(ApiError::new(409, "任务处理身份无效")),
    };
    Ok(json!({"name":identity["client_name"],"agent_type":identity["agent_type"],"kind":kind}))
}
