WITH RECURSIVE inputs(id) AS (
 SELECT id FROM entity_versions WHERE tenant_id = :'tenant' AND id = :'assessment'
 UNION
 SELECT d.input_id FROM dependencies d JOIN inputs p ON d.dependent_id = p.id
 WHERE d.tenant_id = :'tenant'
)
SELECT e.id, e.kind FROM inputs p JOIN entity_versions e ON e.id = p.id
WHERE e.tenant_id = :'tenant' ORDER BY e.id;
