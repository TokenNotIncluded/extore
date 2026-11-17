WITH inventory AS (
 SELECT c.id,c.product_id,json_extract(p.config,'$.name') AS product_name,
 c.state,c.created,m.code_suffix,m.batch_id,b.label AS batch_label,m.expires,m.first_verified,
 COALESCE(NULLIF(m.variant_id,''),'default') AS variant_id,
 COALESCE(NULLIF(json_extract(CASE WHEN json_valid(m.variant_snapshot) THEN m.variant_snapshot ELSE '{}' END,'$.name'),''),
 CASE WHEN COALESCE(NULLIF(m.variant_id,''),'default')='default' THEN '默认规格' ELSE m.variant_id END) AS variant_name,
 j.id AS job_id,j.state AS job_state,j.attempt,COALESCE(j.retryable,0) AS retryable,
 COALESCE(j.revealed,0) AS revealed,j.created AS used_at,
 MAX(c.created,COALESCE(j.updated,c.created),COALESCE(m.first_verified,c.created)) AS updated,
 CASE WHEN c.state='revoked' THEN 'revoked'
 WHEN c.state='rejected' OR j.state='rejected' THEN 'rejected'
 WHEN j.state='destroyed' THEN 'destroyed'
 WHEN j.state IN ('queued','processing','succeeded') THEN j.state
 WHEN j.state='failed' AND j.retryable=1 AND COALESCE(json_extract(p.config,'$.allow_retry'),1)=1
 AND j.attempt-j.revision_start_attempt+1<COALESCE(json_extract(p.config,'$.max_attempts'),3)
 AND (m.expires IS NULL OR m.expires>:now) THEN 'failed_retryable'
 WHEN j.state='failed' THEN 'failed_terminal'
 WHEN (j.id IS NULL OR j.state='needs_input') AND m.expires IS NOT NULL AND m.expires<=:now THEN 'expired'
 WHEN j.state='needs_input' THEN 'needs_input' ELSE 'unused' END AS status
 FROM cards c JOIN products p ON p.id=c.product_id
 LEFT JOIN card_meta m ON m.card_id=c.id
 LEFT JOIN card_batches b ON b.id=m.batch_id
 LEFT JOIN jobs j ON j.card_id=c.id
 WHERE (:product_id='' OR c.product_id=:product_id) AND (:shop_id='' OR p.shop_id=:shop_id)
)
