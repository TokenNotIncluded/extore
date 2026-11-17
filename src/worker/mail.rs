use super::*;
use lettre::{Message,SmtpTransport,Transport,message::{Mailbox,MultiPart,SinglePart},transport::smtp::authentication::Credentials};

fn send(config:&Value,payload:&Value,mid:&str)->std::result::Result<(),(&'static str,bool)> {
    let invalid=||("invalid_mail_payload",true);
    let allowed=["to","subject","body","html"];
    if payload.as_object().is_none_or(|v|v.len()<3||v.len()>4||v.keys().any(|k|!allowed.contains(&k.as_str()))) {return Err(invalid());}
    let to=auth::account::normalize_email(text(payload,"to")).map_err(|_|invalid())?;
    let subject=text(payload,"subject").trim();let body=payload["body"].as_str().ok_or_else(invalid)?;let html=if payload.get("html").is_some(){Some(payload["html"].as_str().ok_or_else(invalid)?)}else{None};
    if subject.is_empty()||subject.chars().count()>200||subject.chars().any(|c|c.is_control())||body.len()+html.map(str::len).unwrap_or(0)>65536||body.contains('\0')||html.is_some_and(|h|h.contains('\0')){return Err(invalid());}
    let from=text(config,"sender").parse().map_err(|_|invalid())?;let to=to.parse().map_err(|_|invalid())?;
    let domain=text(config,"sender").rsplit('@').next().ok_or_else(invalid)?;
    let builder=Message::builder().from(Mailbox::new(Some(text(config,"from_name").to_owned()),from)).to(Mailbox::new(None,to)).subject(subject).message_id(Some(format!("<{mid}@{domain}>")));
    let message=if let Some(html)=html{builder.multipart(MultiPart::alternative().singlepart(SinglePart::plain(body.to_owned())).singlepart(SinglePart::html(html.to_owned())))}else{builder.body(body.to_owned())}.map_err(|_|invalid())?;
    let builder=match text(config,"mode"){"ssl"=>SmtpTransport::relay(text(config,"host")),"starttls"=>SmtpTransport::starttls_relay(text(config,"host")),_=>return Err(invalid())}.map_err(|_|("tls_failed",false))?;
    let mut builder=builder.port(integer(config,"port",465) as u16).timeout(Some(Duration::from_secs(10)));
    if !text(config,"username").is_empty(){builder=builder.credentials(Credentials::new(text(config,"username").to_owned(),text(config,"password").to_owned()));}
    builder.build().send(&message).map_err(|e|(if e.is_tls(){"tls_failed"}else{"transport_failed"},e.is_permanent()))?;Ok(())
}
pub(super) fn once(app:Arc<App>)->Result<bool> {
    let selected=transaction(app.clone(),|ctx|{
        let time=now();ctx.conn.execute("DELETE FROM mail_outbox WHERE id IN(SELECT id FROM mail_outbox WHERE state IN ('delivered','dead') AND created<? ORDER BY created LIMIT 100)",[time-30.0*86400.0])?;
        ctx.conn.execute("UPDATE mail_outbox SET state='dead',payload='',error='expired',lease_until=NULL,lease_token=NULL WHERE state IN ('pending','sending') AND expires<=?",[time])?;
        ctx.conn.execute("UPDATE mail_outbox SET state=CASE WHEN attempts>=8 THEN 'dead' ELSE 'pending' END,payload=CASE WHEN attempts>=8 THEN '' ELSE payload END,error='delivery_interrupted',lease_until=NULL,lease_token=NULL WHERE state='sending' AND lease_until<=?",[time])?;
        let config=auth::account::smtp_config(ctx)?;if !flag(&config,"enabled"){return Ok(None);}
        let Some(r)=row(&ctx.conn,"SELECT * FROM mail_outbox WHERE state='pending' AND due<=? AND expires>? ORDER BY due,created LIMIT 1",params![time,time])?else{return Ok(None)};
        let lease=token();ctx.conn.execute("UPDATE mail_outbox SET state='sending',attempts=attempts+1,lease_until=?,lease_token=? WHERE id=? AND state='pending'",params![time+120.0,lease,text(&r,"id")])?;Ok(Some((r,config,lease)))
    })?;
    let Some((r,config,lease))=selected else{return Ok(false)};
    let payload=auth::secret::open_secret(&app,r["tenant_id"].as_str(),"mail",text(&r,"id"),text(&r,"payload"));
    let result=match payload{Ok(v)=>send(&config,&v,text(&r,"id")),Err(_)=>Err(("invalid_mail_payload",true))};let c=app.db()?;
    match result{Ok(())=>{c.execute("UPDATE mail_outbox SET state='delivered',payload='',error='',lease_until=NULL,lease_token=NULL WHERE id=? AND state='sending' AND lease_token=?",params![text(&r,"id"),lease])?;},Err((error,permanent))=>{let attempts=integer(&r,"attempts",0)+1;let due=now()+(30*2_i64.pow(attempts.min(20) as u32)).min(3600) as f64;let dead=permanent||attempts>=8||due>=number(&r,"expires");c.execute("UPDATE mail_outbox SET state=?,payload=CASE WHEN ? THEN '' ELSE payload END,due=?,error=?,lease_until=NULL,lease_token=NULL WHERE id=? AND state='sending' AND lease_token=?",params![if dead{"dead"}else{"pending"},dead,due,error,text(&r,"id"),lease])?;}}
    Ok(true)
}
