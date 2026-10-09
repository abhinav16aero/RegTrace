SELECT c.input_id AS control_id, string_agg(DISTINCT o.input_id, ',' ORDER BY o.input_id) AS obligations
FROM dependencies c JOIN dependencies o
 ON c.tenant_id = o.tenant_id AND c.dependent_id = o.dependent_id
WHERE c.tenant_id = :'tenant' AND c.relation = 'maps_control' AND o.relation = 'maps_obligation'
GROUP BY c.input_id HAVING count(DISTINCT o.input_id) > 1 ORDER BY c.input_id;
