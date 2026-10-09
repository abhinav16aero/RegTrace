WITH RECURSIVE affected(id) AS (
 SELECT id FROM entity_versions WHERE tenant_id = :'tenant' AND id = :'obligation'
 UNION
 SELECT d.dependent_id FROM dependencies d JOIN affected a ON d.input_id = a.id
 WHERE d.tenant_id = :'tenant'
), impacted_controls AS (
 SELECT d.input_id AS id FROM dependencies d JOIN affected a ON d.dependent_id = a.id
 WHERE d.tenant_id = :'tenant' AND d.relation = 'maps_control'
)
SELECT e.id, e.kind FROM entity_versions e
WHERE e.tenant_id = :'tenant' AND e.id IN (SELECT id FROM affected UNION SELECT id FROM impacted_controls)
ORDER BY e.id;
